import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import yaml
from typing import Set, List, Dict, Optional
import logging

# --- 全局配置 ---
# 日志记录器配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# v2fly 规则的根 URL
V2FLY_BASE_URL = "https://raw.githubusercontent.com/v2fly/domain-list-community/refs/heads/master/data/"

# --- RuleProcessor 类 ---
class RuleProcessor:
    """
    规则处理器类
    负责下载、处理、转换和保存 Clash 规则文件
    """
    def __init__(self, max_workers: int = 20):
        # 初始化带有重试机制的 HTTP 会话
        self.session = self._create_session(max_workers)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        # 规则类型映射
        self.v2fly_rule_map = {
            'full': 'DOMAIN',
            'domain': 'DOMAIN-SUFFIX',
            'keyword': 'DOMAIN-KEYWORD',
        }

    @staticmethod
    def _create_session(pool_size: int) -> requests.Session:
        """
        创建一个配置了重试机制的 HTTP 会话
        
        重试配置:
        - 最多重试 5 次
        - 退避因子 0.5（每次重试等待时间递增）
        - 对指定的 HTTP 状态码进行重试
        
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
            pool_connections=pool_size,
            pool_maxsize=pool_size
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def download_file(self, url: str) -> Optional[str]:
        """从指定 URL 下载文件内容"""
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            # 明确指定 UTF-8 解码
            response.encoding = 'utf-8'
            return response.text
        except requests.RequestException as e:
            logger.error(f"Error downloading {url}: {e}")
            return None

    def download_binary_file(self, url: str, dest_path: str):
        """
        从 URL 下载二进制文件并保存到指定路径。
        针对 .dat 和 .mmdb 等文件。
        """
        try:
            # 确保目标目录存在
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            
            logger.info(f"Downloading: {os.path.basename(dest_path)} from {url}")
            # 使用更长的超时时间并流式传输，适合大文件
            response = self.session.get(url, timeout=120, stream=True)
            response.raise_for_status()

            # 以二进制写模式打开文件
            with open(dest_path, 'wb') as f:
                # 使用 iter_content 逐块写入，减少内存占用
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f" -> Successfully downloaded {os.path.basename(dest_path)}")
            return True
        except requests.RequestException as e:
            logger.error(f" -> Error downloading {url}: {e}")
            return False
        except Exception as e:
            logger.error(f" -> An error occurred while saving {dest_path}: {e}")
            return False

    def read_local_file(self, file_path: str) -> Optional[str]:
        """读取本地文件内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading local file {file_path}: {e}")
            return None

    def _parse_v2fly_line(self, line: str) -> Optional[str]:
        """解析单行 v2fly 格式的规则"""
        line = line.strip()
        if not line or line.startswith('#'):
            return None

        parts = line.split(':', 1)
        if len(parts) == 2 and parts[0] in self.v2fly_rule_map:
            rule_type_source, value = parts
            clash_rule_type = self.v2fly_rule_map.get(rule_type_source.lower())
            return f"{clash_rule_type},{value.strip()}"
        
        # 处理没有前缀的裸域名，默认为 DOMAIN-SUFFIX
        return f"DOMAIN-SUFFIX,{line}"

    def _recursive_parse_v2fly(self, content: str, processed_includes: Set[str]) -> Set[str]:
        """
        递归地解析 v2fly 内容，处理 include 指令
        
        Args:
            content (str): 当前文件内容
            processed_includes (Set[str]): 已处理过的 include 名称，防止循环引用
        
        Returns:
            Set[str]: 解析出的 Clash 规则集合
        """
        rules = set()
        includes_to_fetch = set()

        # 第一遍：分离规则和新的 includes
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            if line.startswith('include:'):
                include_name = line.split(':', 1)[1].strip()
                if include_name not in processed_includes:
                    processed_includes.add(include_name)
                    includes_to_fetch.add(include_name)
            else:
                parsed_rule = self._parse_v2fly_line(line)
                if parsed_rule:
                    rules.add(parsed_rule)

        # 第二遍：并发下载并递归解析 includes
        if includes_to_fetch:
            future_to_include = {
                self.executor.submit(self.download_file, f"{V2FLY_BASE_URL}{name}"): name
                for name in includes_to_fetch
            }
            
            for future in as_completed(future_to_include):
                include_name = future_to_include[future]
                try:
                    include_content = future.result()
                    if include_content:
                        logger.info(f"  -> Processing include: {include_name}")
                        # 递归调用
                        nested_rules = self._recursive_parse_v2fly(include_content, processed_includes)
                        rules.update(nested_rules)
                    else:
                        logger.warning(f"  -> Failed to download include: {include_name}")
                except Exception as e:
                    logger.error(f"  -> Error processing include {include_name}: {e}")
                    
        return rules
        
    def process_v2fly_category(self, url: str) -> Set[str]:
        """
        处理 v2fly 规则的入口方法。
        
        Args:
            url (str): 初始 v2fly 规则文件的 URL
        
        Returns:
            Set[str]: 解析出的所有规则（包括所有递归的 include）
        """
        initial_content = self.download_file(url)
        if not initial_content:
            logger.error(f"Could not start processing, failed to download initial file: {url}")
            return set()
        
        # 初始化一个集合来跟踪已处理的 include，以防无限递归
        processed_includes = set()
        return self._recursive_parse_v2fly(initial_content, processed_includes)

    @staticmethod
    def process_yaml_content(content: str) -> Set[str]:
        """处理标准 Clash YAML 格式的规则内容"""
        if not content:
            return set()
            
        try:
            data = yaml.safe_load(content)
            if not isinstance(data, dict) or 'payload' not in data:
                return set()
                
            rules = set()
            for rule in data['payload']:
                if isinstance(rule, str) and not rule.startswith('#'):
                    rules.add(rule.strip())
            return rules
        except yaml.YAMLError as e:
            logger.error(f"Error parsing YAML content: {e}")
            return set()

    def process_yaml_sources(self, sources: List[str]) -> Set[str]:
        """并发处理多个标准 YAML 规则源"""
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
                        rules = self.process_yaml_content(content)
                        all_rules.update(rules)
                        logger.info(f"Processed {source}: found {len(rules)} rules")
                except Exception as e:
                    logger.error(f"Error processing {source}: {e}")
                    
        return all_rules

    def save_to_file(self, rules: Set[str], filename: str):
        """将规则集合保存到文件，并报告变更"""
        merged_dir = os.path.join('./rules', 'merged')
        os.makedirs(merged_dir, exist_ok=True)
        file_path = os.path.join(merged_dir, filename)
        
        original_rules = set()
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if data and 'payload' in data:
                        original_rules = set(data['payload'])
            except Exception as e:
                logger.error(f"Error reading existing file {filename}: {e}")
        
        # 排序后的列表用于比较和写入，保证一致性
        sorted_rules = sorted(list(rules))
        sorted_original_rules = sorted(list(original_rules))
        
        if sorted_rules == sorted_original_rules:
            logger.info(f"{filename}: No changes detected. Total rules: {len(sorted_rules)}")
            return
            
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(
                    {'payload': sorted_rules},
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False
                )
            
            rules_added = rules - original_rules
            rules_removed = original_rules - rules
            
            logger.info(f"{filename}: Updated! Total rules: {len(rules)}")
            if rules_added:
                logger.info(f"  - Added {len(rules_added)} rules.")
            if rules_removed:
                logger.info(f"  - Removed {len(rules_removed)} rules.")
        except Exception as e:
            logger.error(f"Error writing to {file_path}: {e}")

# --- 主流程 ---
def main():
    """主函数：协调整个规则处理流程"""
    start_time = time.time()
    processor = RuleProcessor()
    
    # 【新增】v2fly 规则源配置
    v2fly_sources = {
        'category-game-platforms-download': f'{V2FLY_BASE_URL}category-game-platforms-download',
        'category-games-!cn': f'{V2FLY_BASE_URL}category-games-!cn',
        'category-games-cn': f'{V2FLY_BASE_URL}category-games-cn'
    }

    # 【新增】处理 v2fly 规则
    for category, url in v2fly_sources.items():
        logger.info(f"\nProcessing v2fly category: {category}")
        try:
            rules = processor.process_v2fly_category(url)
            processor.save_to_file(rules, f'{category}.yaml')
        except Exception as e:
            logger.error(f"FATAL: Error processing v2fly category {category}: {e}")
            continue

    # 规则分类配置 (标准 YAML 源)
    categories = {
        'LocalAreaNetwork': ["https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/LocalAreaNetwork.yaml"],
        # ... 你原有的其他 categories ...
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
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/refs/heads/master/Clash/Providers/Ruleset/NetEaseMusic.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/AsianMedia/AsianMedia.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/XiaoHongShu/XiaoHongShu.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/DouYin/DouYin.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/JingDong/JingDong.yaml"

        ],
        'proxy': [
            "https://github.com/blackmatrix7/ios_rule_script/raw/master/rule/Clash/Proxy/Proxy.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ProxyGFWlist.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/ProxyLite.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Developer.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Github.yaml",
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/Google.yaml",
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
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Game/GameDownloadCN/GameDownloadCN.yaml",
            "./rules/custom/weishi_direct.yaml"
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
            "./rules/custom/upload.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/XiaoMi/XiaoMi.yaml"
        ],
        'TikTok': [
            "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/Providers/Ruleset/TikTok.yaml",
            "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/TikTok/TikTok.yaml"
        ]
    }
    
    # 处理每个标准 YAML 规则类别
    for category, sources in categories.items():
        logger.info(f"\nProcessing YAML category: {category}")
        try:
            rules = processor.process_yaml_sources(sources)
            processor.save_to_file(rules, f'{category}.yaml')
        except Exception as e:
            logger.error(f"FATAL: Error processing YAML category {category}: {e}")
            continue
    
    # --- 【新增】下载 .dat 和 .mmdb 文件 ---
    logger.info("\nProcessing binary GEO files...")
    geo_files_to_download = [
        {
            'url': 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat',
            'filename': 'geoip.dat'
        },
        {
            'url': 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat',
            'filename': 'geosite.dat'
        },
        {
            'url': 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb',
            'filename': 'country.mmdb'
        }
    ]
    
    merged_dir = os.path.join('./rules', 'merged')
    
    # 使用独立的线程池并发下载
    with ThreadPoolExecutor(max_workers=3) as geo_executor:
        future_to_url = {
            geo_executor.submit(
                processor.download_binary_file, 
                file_info['url'], 
                os.path.join(merged_dir, file_info['filename'])
            ): file_info['url'] 
            for file_info in geo_files_to_download
        }
        
        # 等待所有下载任务完成，并捕获可能的异常
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                # .result() 会重新引发在线程中发生的异常
                future.result()
            except Exception as exc:
                logger.error(f"An exception occurred during download from {url}: {exc}")

    # 输出总耗时
    logger.info(f"\nAll tasks completed in {time.time() - start_time:.2f} seconds")

if __name__ == "__main__":
    # 确保依赖已安装: pip install requests pyyaml
    main()
