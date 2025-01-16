# Rule Provider

这是一个统一的Clash规则提供者项目，整合了个人规则和在线规则。

## 项目结构

```
rule-provider/
├── rules/
│   ├── custom/           # 个人自定义规则
│   │   ├── proxy.yaml    # 个人代理规则
│   │   ├── direct.yaml   # 个人直连规则
│   │   └── ...
│   └── merged/          # 合并后的规则
│       ├── proxy.yaml
│       ├── direct.yaml
│       └── ...
├── scripts/
│   └── merge.py         # 规则合并脚本
└── .github/
    └── workflows/
        └── update.yml   # GitHub Actions 工作流
```

## 使用方法

1. 修改个人规则：
   - 直接编辑 `rules/custom/` 目录下的相应文件
   - 提交更改后，GitHub Actions 会自动合并规则并更新 `rules/merged/` 目录

2. 使用规则：
   - 个人规则：直接引用 `rules/custom/` 下的文件
   - 合并规则：使用 `rules/merged/` 下的文件

## 自动化流程

- 每次推送到主分支时自动执行合并
- 每天定时从在线源更新规则
- 合并完成后自动部署更新 