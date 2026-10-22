# 输出策略

- 正式产物必须由 `innovation:report generate` 或批准的产物 Skill 生成。
- 默认中间输出应放在 `runtime/outputs/`，不要把中间产物塞进 media。
- 面向用户的文件必须放在当前会话的 `media/local-file/` 目录，不得落在工作区根目录或其他位置。
- 每个生成的文件必须在结果中记录完整的文件名和路径。
- 回复用户时必须展示完整文件名，不得只写"已生成"而不给文件名或路径。
- 在存在性、非空、可读性和内容检查通过之前，不要编造公共 URL 或声称产物已就绪。
- 禁止编造公网 URL、`MEDIA:` 路径，禁止把文件名做成 markdown 链接。

## 会话目录规则

交付文件路径为 `media/local-file/{dynamicSubdir}/{filename}`，目录交付为 `media/local-file/{dynamicSubdir}/{dirname}/`。

`{dynamicSubdir}` = 当前 sessionKey 去掉开头 `agent:<agentName>:` 两段后的**剩余部分，原样作为目录名**：

- 不得对剩余部分做任何增删、替换或转义，**包括其中的冒号**（目录名含冒号在本系统是合法的，必须保留）；
- 示例：sessionKey `agent:xxx:dingtalk-connector:group:cidXXXX` → `dingtalk-connector:group:cidXXXX`；sessionKey `agent:xxx:dashboard:7f71b8a1-...` → `dashboard:7f71b8a1-...`（以当前真实 sessionKey 计算，不要照抄示例值）；
- 错误做法：只取最后一段 UUID 建目录、把冒号换成下划线或斜杠、自造其他目录名。平台靠该目录名把文件关联回会话，目录名不对，文件就不会出现在用户的"生成文件"面板里。

## 交付话术

- 用户要"下载 / 链接"时：说明文件已落盘，请到"生成文件"面板下载。
- Dashboard **不要**用 `message` + `media` 发附件（附件 URL 是 Pod 路径，点开下不了）。
- 工具返回的文件信息缺少文件名或路径时，不要发送空白下载条目；先补齐元信息再回复。