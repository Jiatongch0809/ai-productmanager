---
name: gpt-image-relay
description: 通过公网 OpenAI-compatible 中转调用 gpt-image-2 文生图并落盘 PNG。Use when 用户要生图、画 Logo、出 PNG/海报，或 image_generate 报 SSRF / No image-generation provider registered。
---

# gpt-image-relay

不要用 OpenClaw 原生 `image_generate`（`openai/*` 会打内网 Gateway 被 SSRF 拦；`gpt-image/*` 未注册生图 Provider）。

用本目录脚本直连公网中转。凭据只读环境变量，不要把 API Key 写进会话或文件。

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `GPT_IMAGE_BASE_URL` | 是 | 中转根路径，须含 `/v1`，例如 `https://your-relay.example.com/v1` |
| `GPT_IMAGE_API_KEY` | 是 | 中转 API Key |

未注入时脚本退出码 2。这两项由实例 `environment_overrides_json` 下发，改库后须重启 Pod。

## 用法

在工作区执行（路径按本 skill 实际目录）：

```bash
python3 <workspace>/skills/gpt-image-relay/scripts/generate.py \
  --prompt "一只红色小龙虾图标，白色背景，无文字" \
  --out media/test-gpt-image-relay.png
```

可选：`--size 1024x1024`（默认）、`--model gpt-image-2`。

成功则 stdout 打印 PNG 绝对路径。失败则把脚本 stderr 原文回给用户，**禁止**改用 `write` 手写 SVG 交差。
