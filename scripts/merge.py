import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import yaml
from typing import Set, List, Dict
import logging

# 配置日志记录器
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RuleProcessor:
    """
    规则处理器类
    负责下载、处理和保存 Clash 规则文件
    """
    def __init__(self):
        # 初始化带有重试机制的 HTTP 会话
        self.session = self._create_session()
        
    @staticmethod
    def _create_session() -> requests.Session:
        """
        创建一个配置了重试机制的 HTTP 会话
        
        重试配置:
        - 最多重试 5 次
        - 退避因子 0.5（每次重试等待时间递增）
        - 对指定的 HTTP 状态码进行重试
        - 连接池最大连接数 20
        
        Returns:
            requests.Session: 配置好的会话对象
        """
        session = requests.Session()
        retries = Retry(
            total=5,  # 最大重试次数
            backoff_factor=0.5,  # 重试间隔时间的增长因子
            status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的 HTTP 状态码
            allowed_methods=["GET"]  # 允许重试的 HTTP 方法
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=20,  # 连接池最大连接数
            pool_maxsize=20  # 连接池最大大小
        )
        # 为 HTTP 和 HTTPS 配置重试适配器
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def download_file(self, url: str) -> str:
        """
        从指定 URL 下载文件内容
        
        Args:
            url: 要下载的文件 URL
            
        Returns:
            str: 文件内容，下载失败则返回 None
        """
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.error(f"Error downloading {url}: {e}")
            return None

    def read_local_file(self, file_path: str) -> str:
        """
        读取本地文件内容
        
        Args:
            file_path: 本地文件路径
            
        Returns:
            str: 文件内容，读取失败则返回 None
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading local file {file_path}: {e}")
            return None

    @staticmethod
    def process_content(content: str) -> Set[str]:
        """
        处理 YAML 格式的规则内容
        
        Args:
            content: YAML 格式的规则内容
            
        Returns:
            Set[str]: 处理后的规则集合（已去重）
        """
        if not content:
            return set()
            
        try:
            # 解析 YAML 内容
            data = yaml.safe_load(content)
            if not isinstance(data, dict) or 'payload' not in data:
                return set()
                
            # 过滤规则：去除注释和空白字符，并去重
            rules = set()
            for rule in data['payload']:
                if isinstance(rule, str) and not rule.startswith('#'):
                    rules.add(rule.strip())
            return rules
            
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML content: {e}")
            return set()

    def process_sources(self, sources: List[str]) -> Set[str]:
        """
        并发处理多个规则源（支持远程 URL 和本地文件）
        
        Args:
            sources: 规则源列表（URL 或本地文件路径）
            
        Returns:
            Set[str]: 合并后的规则集合
        """
        all_rules = set()
        
        # 使用线程池并发处理规则源
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_source = {}
            
            # 提交下载或读取任务到线程池
            for source in sources:
                if source.startswith(('http://', 'https://')):
                    # 处理远程URL
                    future = executor.submit(self.download_file, source)
                else:
                    # 处理本地文件
                    future = executor.submit(self.read_local_file, source)
                future_to_source[future] = source
            
            # 收集处理结果
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    content = future.result()
                    if content:
                        rules = self.process_content(content)
                        all_rules.update(rules)
                        logger.info(f"Processed {source}: found {len(rules)} rules")
                except Exception as e:
                    logger.error(f"Error processing {source}: {e}")
                    
        return all_rules

    def save_to_file(self, rules: Set[str], filename: str):
        """
        将规则保存到文件
        
        Args:
            rules: 要保存的规则集合
            filename: 目标文件名
        """
        # 创建保存目录
        merged_dir = os.path.join('./rules', 'merged')
        os.makedirs(merged_dir, exist_ok=True)
        
        file_path = os.path.join(merged_dir, filename)
        original_count = 0
        
        # 读取现有文件的规则数量（如果存在）
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if data and 'payload' in data:
                        original_count = len(data['payload'])
            except Exception as e:
                logger.error(f"Error reading existing file {filename}: {e}")
        
        # 保存新规则
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(
                    {'payload': sorted(rules)},  # 规则排序后保存
                    f,
                    allow_unicode=True,  # 允许 Unicode 字符
                    default_flow_style=False,  # 使用块格式而不是流格式
                    sort_keys=False  # 不对键进行排序
                )
            
            new_count = len(rules)
            self._log_update_status(filename, original_count, new_count)
            
        except Exception as e:
            logger.error(f"Error writing to {file_path}: {e}")

    @staticmethod
    def _log_update_status(filename: str, original_count: int, new_count: int):
        """
        记录规则更新状态
        
        Args:
            filename: 文件名
            original_count: 原规则数量
            new_count: 新规则数量
        """
        if original_count == 0:
            logger.info(f"{filename}: Created with {new_count} rules")
        elif original_count != new_count:
            logger.info(f"{filename}: Updated from {original_count} to {new_count} rules")
            logger.info(f"{filename}: {'#' * 20}")
            logger.info(f"{filename}: ##### Updated #####")
            logger.info(f"{filename}: {'#' * 20}")
        else:
            logger.info(f"{filename}: No changes detected")

def download_additional_files():
    """
    下载额外的规则文件
    这些文件使用不同的格式，需要单独处理
    """
    additional_files = {
        'CNDomain.list': 'https://app.nloli.xyz/static/subrule/clash_rule/CNDomain.list',
        'Proxy.list': 'https://app.nloli.xyz/static/subrule/Proxy.list'
    }
    
    # 创建保存目录
    merged_dir = os.path.join('./rules', 'merged')
    os.makedirs(merged_dir, exist_ok=True)
    
    # 下载并保存文件
    session = requests.Session()
    for filename, url in additional_files.items():
        try:
            response = session.get(url, timeout=10)
            response.raise_for_status()
            
            file_path = os.path.join(merged_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(response.content)
            logger.info(f"Successfully downloaded {filename}")
            
        except Exception as e:
            logger.error(f"Error downloading {filename} from {url}: {e}")

def main():
    """
    主函数：协调整个规则处理流程
    """
    start_time = time.time()
    processor = RuleProcessor()
    
    # 规则分类配置
    categories = {
        'LocalAreaNetwork': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/LocalAreaNetwork.yaml"
        ],
        'CN': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/ChinaDNS.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/PublicDirectCDN.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/AccelerateDirectSites.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/ChinaNet.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/SteamCN.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Download.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/GameDownload.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/PrivateTracker.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Xunlei.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ChinaMedia.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ChinaDomain.yaml",
            "./rules/custom/weishi_direct.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/Bilibili.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/ByteDance.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/NetEaseMusic.yaml"
        ],
        'proxy': [
            "https://github.com/blackmatrix7/ios_rule_script/raw/master/rule/Clash/Proxy/Proxy.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ProxyGFWlist.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ProxyLite.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Developer.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Github.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Google.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Apple.yaml",
            "./rules/custom/proxy.yaml"
        ],
        'weishi_proxy': [
            "./rules/custom/proxy.yaml"
        ],
        'Netflix': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Netflix.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/NetflixIP.yaml",
            "https://raw.githubusercontent.com/dler-io/Rules/master/Clash/Provider/Media/Netflix.yaml",
            "https://github.com/blackmatrix7/ios_rule_script/raw/master/rule/Clash/Netflix/Netflix.yaml"
        ],
        'Telegram': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Telegram.yaml",
            "https://raw.githubusercontent.com/dler-io/Rules/main/Clash/Provider/Telegram.yaml",
            "https://github.com/blackmatrix7/ios_rule_script/raw/master/rule/Clash/Telegram/Telegram.yaml"
        ],
        'Youtube': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/YouTube.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ProxyMedia.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Telegram.yaml",
            "https://raw.githubusercontent.com/dler-io/Rules/main/Clash/Provider/Telegram.yaml",
            "https://github.com/blackmatrix7/ios_rule_script/raw/master/rule/Clash/Telegram/Telegram.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Pixiv.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Porn.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/PornAsia.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Pornhub.yaml",
            "https://raw.githubusercontent.com/dler-io/Rules/main/Clash/Provider/Media/Pornhub.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Twitch.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/AppleNews.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/AppleTV.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/Instagram.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/Twitter.yaml",
            "./rules/custom/media.yaml"
        ],
        'GameDownload': [
            "./rules/custom/GameDownload.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/GameDownload.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Game/GameDownloadCN/GameDownloadCN.yaml"
        ],
        'VPS': [
            "./rules/custom/vps.yaml"
        ],
        'UnBan': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/UnBan.yaml"
        ],
        'AD': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/BanAD.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/BanEasyList.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/BanEasyListChina.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/BanEasyPrivacy.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/BanProgramAD.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/MIUIPrivacy.yaml"
        ],
        'anti_ad': [
            "https://raw.githubusercontent.com/privacy-protection-tools/anti-AD/master/anti-ad-clash.yaml"
        ],
        'openAI': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/OpenAi.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Claude.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/ClaudeAI.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Anthropic/Anthropic.yaml"
        ],
        'Microsoft': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Microsoft.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Microsoft/Microsoft.yaml"
        ],
        'OneDrive': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/OneDrive.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/OneDrive/OneDrive.yaml"
        ],
        'pdr': [
            "./rules/custom/pdr.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/OpenAi.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Claude.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/ClaudeAI.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Anthropic/Anthropic.yaml"
        ],
        'decipher': [
            "./rules/custom/decipher.yaml"
        ],
        'upload': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/Baidu.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/PrivateTracker.yaml",
            "./rules/custom/upload.yaml"
        ],
        'TikTok': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/TikTok.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/TikTok/TikTok.yaml"
        ]
    }
    
    # 处理每个规则类别
    for category, sources in categories.items():
        logger.info(f"\nProcessing category: {category}")
        try:
            rules = processor.process_sources(sources)
            processor.save_to_file(rules, f'{category}.yaml')
        except Exception as e:
            logger.error(f"Error processing category {category}: {e}")
            continue

    # 下载额外的规则文件
    logger.info("\nDownloading additional files...")
    try:
        download_additional_files()
    except Exception as e:
        logger.error(f"Error downloading additional files: {e}")

    # 输出总耗时
    logger.info(f"\nCompleted in {time.time() - start_time:.2f} seconds")

if __name__ == "__main__":
    main()
