#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给打包好的 exe 签名，生成签名清单 dist/Tuoz1Companion.exe.sig.json。发版流程（release.yml）里自动跑。

为什么要签：连不上 GitHub 的玩家，插件会改从机器人下载新版本（明文 HTTP），下载下来还要以管理员身份运行。
没有签名的话，任何能篡改玩家网络的人都能让他的电脑运行任意程序。所以每个发布的 exe 都用私钥签名，
插件里内置公钥（RELEASE_PUBKEY_N），签名或文件哈希对不上就拒绝更新。机器人只负责搬运，不参与签名。

私钥从环境变量 RELEASE_SIGNING_KEY 读（PEM 文本）：在 GitHub 上是仓库的 Actions 密钥；
作者电脑上的备份在 %USERPROFILE%\\.tuoz1bot\\dc_release_private_key.pem，也可以用 --key 指给它。
私钥丢了的话：换一把新的要同时改插件里的公钥，已经装着旧公钥的插件就不认新签名了（它们还能从 GitHub 正常更新）。

用法：python tools/sign_release.py [--exe dist/Tuoz1Companion.exe] [--key 私钥文件]
要先装：pip install cryptography
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tuoz1_companion as plugin  # noqa: E402  版本号、被签名内容的拼法、公钥都以插件源码为准


def main() -> int:
    ap = argparse.ArgumentParser(description="给插件 exe 签名")
    ap.add_argument("--exe", default=str(ROOT / "dist" / "Tuoz1Companion.exe"))
    ap.add_argument("--key", help="私钥文件（不给就读环境变量 RELEASE_SIGNING_KEY）")
    args = ap.parse_args()

    pem = Path(args.key).read_bytes() if args.key else (os.environ.get("RELEASE_SIGNING_KEY") or "").encode()
    if not pem.strip():
        print("没有私钥：设置环境变量 RELEASE_SIGNING_KEY，或者用 --key 指定私钥文件")
        return 1
    key = serialization.load_pem_private_key(pem, password=None)
    pub = key.public_key().public_numbers()
    if (pub.n, pub.e) != (plugin.RELEASE_PUBKEY_N, plugin.RELEASE_PUBKEY_E):
        print("这把私钥和插件源码里的公钥（RELEASE_PUBKEY_N）不是一对：签出来的东西插件不会认，不签")
        return 1

    exe = Path(args.exe)
    data = exe.read_bytes()
    version, size, sha256 = plugin.VERSION, len(data), hashlib.sha256(data).hexdigest()
    message = plugin.release_message(version, size, sha256)
    sig = key.sign(message, padding.PKCS1v15(), hashes.SHA256()).hex()
    if not plugin.verify_signed(message, sig):                 # 用插件自己的验签函数再验一遍：签出来的必须是插件认的
        print("签完用插件的验签函数验不过，不发")
        return 1
    out = exe.with_name(exe.name + ".sig.json")
    out.write_text(json.dumps({"version": version, "size": size, "sha256": sha256, "sig": sig}), encoding="utf-8")
    print(f"签好了：{out.name}（版本 {version}，{size} 字节，sha256 {sha256[:16]}…）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
