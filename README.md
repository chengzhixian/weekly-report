# Weekly Report：本机 Codex OKR 周报 Skill

将本机一周内与 Codex 的对话和工作记录整理为中文 OKR 周报。按目标、关键结果、本周进展、问题与下周计划组织内容，适合跨项目的研发工作总结。

## 功能

- 扫描当前用户的 Codex 活动会话及归档会话，按事件时间筛选；较早创建、近期恢复的会话也可纳入。
- 默认使用北京时间，支持任意日期范围。默认仅采集 Codex，Claude 和 GitHub PR 按需补充。
- 默认排除子代理审查记录，可按 ID 排除当前写周报线程。
- 输出最终答复、用户请求和相关证据索引，由 Codex 归纳为 OKR 周报；采集脚本本身不调用模型，也不直接生成最终周报。
- 区分目标值、实际结果和本周增量，不编造完成率；没有正式 OKR 时将归纳目标标为草案。
- 默认 Markdown，可按需使用内置模板生成 HTML。

## 安装到 Codex

需要 Python 3.10+。在一个持久目录克隆，然后建立 skill 链接：

```bash
git clone https://github.com/chengzhixian/weekly-report.git
mkdir -p ~/.codex/skills
ln -s "$(pwd)/weekly-report" ~/.codex/skills/weekly-report
```

如果使用自定义 CODEX_HOME，把链接放到对应的 skills 目录。已有同名 skill 时保留现有目录，按需比较或更新文件；不要直接覆盖个人配置。

该仓库为私有仓库时，需要有访问权限的 GitHub 身份才能克隆。

## 使用

在 Codex 中直接提出：

```text
用 $weekly-report 整理最近一周的 OKR 周报。
用 $weekly-report 整理上周周报，沿用我已有的 O1、O2。
用 $weekly-report 汇总 2026-10-13 到 2026-10-19 的工作。
```

默认输出到 `~/weekly-reports/Weekly-Report-START-to-END.md`。最近一周表示含今天的七个自然日；本周表示周一至今天；上周表示上一周周一至周日。

独立运行采集器：

```bash
python3 scripts/collect_codex_activity.py   --start 2026-10-13 --end 2026-10-19   --timezone Asia/Shanghai --max-sessions 1000 --format json
```

其他参数：`--max-text` 调整片段长度，`--exclude-session-id ID` 可重复指定，`--include-subagents` 显式纳入子代理。数量和消息长度截断信息会写入采集结果。重要结论仍应回查原始消息、工具结果或交付物。

## OKR 写作约定

1. O 描述希望达到的状态，KR 描述可核验的结果或交付物。
2. 有正式 OKR 时保持周期和编号；没有时将归纳结果标为待确认草案。
3. 本周进展和下周计划均对应 O/KR，用状态标记区分完成、部分完成和待验证。
4. 目标值、实际结果和本周增量分开，缺少依据时不生成百分比。
5. 区分编译、仿真、上板和实机验收；单项通过不扩大为整体完成。

完整原则与流程见 [SKILL.md](SKILL.md)，风格说明见 [references/okr-style.md](references/okr-style.md)。

## 数据范围

采集脚本只读本机日志，使用 Python 标准库，不自动上传、发消息或发布文档。输出片段可能包含原始对话中的敏感内容，因此原始采集结果只作本地证据使用；发布周报前由执行 skill 的 Codex 检查内容。仓库不包含个人会话、真实周报、飞书文档链接或认证信息。

采集器同时检查 CODEX_HOME 和当前用户的 `~/.codex`，并去除重复路径。缺少记录的工作不会自动补齐；助手的成功声明也不能替代验收证据。

## 来源与许可证

基于 [tang-t21/weekly-report](https://github.com/tang-t21/weekly-report) 的 MIT 版本改造，上游提交为 `ea0b0810485949311e4394f20f9a49ec7478e3af`。本版本增加中文 OKR 工作流和本机历史采集修正，保留上游 [MIT 许可证](LICENSE)。
