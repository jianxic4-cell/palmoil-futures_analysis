# 发布检查清单

## 发布内容

发布范围见根目录的 `PUBLIC_FILES.md`：程序、依赖清单、配置模板、文档和离线测试。
不包含真实交易、行情、数据库导出、生成的报告、虚拟环境或临时文件。

## 发布前检查

1. 运行 `git status --short --untracked-files=all`，核对待提交文件。
2. 检查密码、token、账户、个人信息、私有连接地址和本机路径。
3. 排除原始账单、真实成交、账户权益、数据库导出、授权行情和 SDK 配置。
4. `.gitignore` 不保护网页拖放上传、`git add -f` 或已经跟踪的文件。
5. 核对项目与数据的使用权限，并明确项目许可证；当前仓库未包含 `LICENSE` 文件。

## 本地检查与提交

在具备项目依赖的环境中，从仓库根目录执行：

```text
python -m unittest discover -s tests -v
python scripts/demo.py
python scripts/demo_reports.py
git status --short --untracked-files=all
```

确认结果后，在已初始化的 Git 仓库中准备提交：

```text
git add README.md README.zh-CN.md PUBLIC_FILES.md requirements.txt .gitignore .env.example .github docs scripts tests
git diff --cached --stat
git diff --cached
git commit -m "Add futures data analysis and reporting tools"
```

逐项检查暂存内容，并确认提交姓名和邮箱适合公开。远程仓库地址按实际项目配置。
GitHub Actions 在 push 或 pull request 后运行；本地测试记录与远程运行结果分别查看。

## 结果表述

演示数据为合成数据，不是历史行情或真实收益。候选组合数量不等于已经确认的套利次数。
描述收益、回撤或改进比例时，应同时提供实验定义、数据范围和验证依据。
