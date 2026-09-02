# 执行机配置

`agents.example.json` 是中央网关可提交的配置模板。将其复制为 `agents.json` 后填写本机或远程 Codex app-server 信息；`agents.json` 已被 Git 忽略。

`agents.remote-sidecar.example.json` 是远程主监督机本地 Sidecar 使用的执行机清单模板。

app-server 访问令牌通过 `token_env` 或 `token_file` 配置，远程主监督的独立机器令牌通过 `sidecar_token_env` 或 `sidecar_token_file` 配置；每一组都严格二选一，不要把令牌值直接写入 JSON。

中央配置、远程 Sidecar 配置和各机器部署步骤见[完整部署指南](../docs/DEPLOYMENT_GUIDE.zh-CN.md)。
