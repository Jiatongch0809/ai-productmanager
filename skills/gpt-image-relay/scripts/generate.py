#!/usr/bin/env python3
"""Call public OpenAI-compatible image API. Credentials from env only.

【这个脚本做什么】
gpt-image-relay skill 的“文生图”执行步骤：向一个 OpenAI 兼容的 /images/generations 接口发起 POST，
把响应里的 base64 图片解码后写到本地文件。它只负责“转发 prompt + 保存图片”，不做内容校验。

【产物写到哪】
写到 --out 指定的路径；脚本会自动创建其父目录（os.makedirs）。
它不写 runtime/，也不写任何业务运行数据。

【被谁消费】
上游“逐页生成最终整页 PNG 再拼 PPTX”的流程会为每一页调用本脚本（传 prompt/out/size/model），
拿到的 PNG 路径再交给后续拼装步骤；因此它输出的是中间产物（每页一张图），不是最终交付物。

【凭据来源】
只从环境变量读，绝不写死在代码或命令行参数里：
  GPT_IMAGE_BASE_URL —— 接口基址（脚本会先 strip() 去首尾空格、再 rstrip("/") 去尾斜杠）；
  GPT_IMAGE_API_KEY  —— API Key。
两者缺一即直接返回失败（退出码 2），不会发起网络请求。

【退出码约定】（调用方据此判断成败）
  0 = 成功，图片已写入 --out，并把绝对路径打印到 stdout；
  1 = 请求失败 / 响应不是合法 JSON / 响应里没有图片数据；
  2 = 缺少环境变量（配置问题，与网络无关）。

【安全】
日志只打印错误正文与路径，不打印 API Key；Key 仅出现在 Authorization 请求头里。
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    """解析参数 → 校验环境变量 → 发起生图请求 → 解析响应 → 写出图片。

    参数（来自命令行 argparse）：
      --prompt 必填，文生图提示词；
      --out    必填，输出文件路径；
      --size   可选，图片尺寸，默认 1024x1024；
      --model  可选，模型名，默认 gpt-image-2。
    返回：int 退出码（0 成功 / 1 请求或解析失败 / 2 缺环境变量），
    在文件末尾由 sys.exit(main()) 直接作为进程退出码。
    设计：把“参数解析 + 环境变量校验”放在最前，配置缺失立即返回 2，
    从而让调用方可区分“配置问题（2）”与“网络/接口问题（1）”，不必重试网络。
    """
    parser = argparse.ArgumentParser()
    # 定义三个输入参数：prompt/out 必填，size/model 带默认值（与 relay 约定一致，模型默认 gpt-image-2）。
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--model", default="gpt-image-2")
    # 解析命令行；参数不合规时 argparse 会自行报错退出（非本函数的退出码）。
    args = parser.parse_args()

    # 从环境变量读取配置：base url 去掉首尾空格与尾部 “/”，key 去掉首尾空格。
    # 之所以在这里做归一化，是为了容忍用户粘贴时带上的空格/尾斜杠，避免拼出的 URL 出现 “//”。
    base = os.environ.get("GPT_IMAGE_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("GPT_IMAGE_API_KEY", "").strip()
    # 缺任一环境变量即视为配置错误：打印固定文案到 stderr 并返回 2，绝不带着空 Key 去请求。
    if not base or not key:
        print(
            "missing GPT_IMAGE_BASE_URL or GPT_IMAGE_API_KEY",
            file=sys.stderr,
        )
        return 2

    # 拼接文生图端点；请求体里 n=1（每次只生成一张，relay 逐页调用）。
    # ensure_ascii=False 让中文 prompt 在请求体里保持可读（不影响接口正确性）。
    url = base + "/images/generations"
    body = json.dumps(
        {
            "model": args.model,
            "prompt": args.prompt,
            "size": args.size,
            "n": 1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    # 用标准库 urllib 发 POST（不引入第三方依赖）：Content-Type 为 JSON，Authorization 用 Bearer Token。
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    # 发起请求，超时 180 秒（生图较慢，给足时间）；响应体按 UTF-8 解码，非法字节用 replace 容错。
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", "replace")
    # HTTP 错误（4xx/5xx）：把接口返回的错误正文原样打到 stderr，返回 1，便于上游定位（如额度/鉴权失败）。
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace") or str(exc), file=sys.stderr)
        return 1
    # 其他异常（超时/网络不可达等）：打印异常信息，同样返回 1。
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 解析响应：OpenAI 兼容格式为 {"data":[{"b64_json":...}, ...]}，这里只取第 0 个结果。
    # 解析失败则打印响应前 800 字符（截断，避免日志爆炸）并返回 1。
    try:
        payload = json.loads(raw)
        item = (payload.get("data") or [])[0]
        b64 = item.get("b64_json")
    except Exception:
        print(raw[:800], file=sys.stderr)
        return 1
    # 没有 b64 图片数据（例如接口只返回 url、或 data 为空）时同样按失败返回 1。
    if not b64:
        print("no b64_json in response", file=sys.stderr)
        return 1

    # 写文件：先转成绝对路径；父目录可能不存在，故对 dirname 调用 makedirs（dirname 为空时退回 "."）。
    # 图片按二进制写入（base64 解码后直接落盘），假定返回内容就是图片字节。
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(base64.b64decode(b64))
    # 成功：把产物绝对路径打印到 stdout（供上游/调用方解析），返回 0。
    print(out)
    return 0


# 作为命令行脚本运行时，用 main() 的返回值作为进程退出码（0/1/2），方便上游按退出码分支处理。
if __name__ == "__main__":
    sys.exit(main())
