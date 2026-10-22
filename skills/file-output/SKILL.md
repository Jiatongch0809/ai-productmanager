---
name: file-output
description: 生成或读取交付文件时使用。
---

# 文件交付

把最终给用户的文件写入 `media/local-file/{lastSeg}/`。

`lastSeg` = 当前 sessionKey **最后一个 `:` 后面**那一段。

- `agent:a141b:dashboard:ed4b1f38-10dc-487c-b7d7-a7742a206b46` → `ed4b1f38-10dc-487c-b7d7-a7742a206b46`
- 不要加 `dashboard:` 前缀，不要用冒号当目录名。
- 若该段含 `/`，改成 `-`。
- 禁止在 `{lastSeg}` 下再新建子目录。生成文件直接放在这一层，不要写成 `{lastSeg}/某目录/文件`。

## 步骤

1. 从本轮 sessionKey 取出 lastSeg，写入 `media/local-file/{lastSeg}/{文件名}`。
2. 写完后确认文件存在。
3. 回复只带**纯文本完整文件名**，并说明到「生成文件」面板下载。
4. 中间产物写 `runtime/outputs/<任务>/<日期>/`，不要进 `media/`。

## 禁止

- 禁止 markdown 链接、公网 URL、`MEDIA:` Pod 路径。
- Dashboard 不要用 `message`+`media` 发附件。
- 禁止写到 workspace 根。
