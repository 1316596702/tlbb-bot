# 发布检查

当前目录仅为本地候选，尚未关联远程仓库。发布前确认：

1. `python -m unittest discover -s tests -v` 和 `python tools/check_release.py` 通过。
2. 只将本目录纳入新的 Git 仓库，不复制开发历史、上层目录、运行目录和审计资料。
3. 审查 `git status --short --untracked-files=all` 与 `git diff --cached`，包括 .github 隐藏文件。
4. 不能只依赖 .gitignore：已经跟踪的文件也要扫描；从未提交过口令才能保证初始历史无泄漏。
5. 确认 LICENSE、NOTICE、依赖说明、贡献规范齐全。不要添加 MIT/GPL 徽章声称允许商业使用。
6. 选择 GitHub 账号/组织，创建名为 tlbb-bot 的仓库；描述为“TLBB 智能假人组队刷马系统｜非商业源码公开”。
7. 首个版本标记为预览，写明尚未适配的接口与没有执行过的验证。
8. 启用 Issues、私密漏洞报告和 main 分支保护；按需启用 Discussions，标注 good first issue、help wanted、bug、version-adapter。

发布可以采用源码仓库；在可复现构建与依赖审计完成前，不附带现成 EXE。公开后已有 Fork 和下载副本可能继续存在，删除仓库不能替代发布前脱敏。

许可与协作资料参考 GitHub 官方文档：
- https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository
- https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors
- https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository
