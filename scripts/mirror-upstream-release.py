#!/usr/bin/env python3
"""把上游 DSH-APP/DSHA 的 Release 资产镜像到本仓库。

为什么这么写（每条都对应一个踩过的坑或一条必须守住的底线）：

1. 不重新打包、不重新签名。只搬运上游官方产物，sha256 与上游逐字节一致；
   镜像站一旦自己重签，用户装上去就跟官方更新链断开了。
2. 先验后发。下载后必须通过「GitHub 给上游资产算的 digest」和「上游 .sha256 旁车」
   两道校验之一（有旁车就两道都要过）；一道都不通就整轮失败、什么都不上传。
   宁可不镜像，也不镜像一个来源不明的 APK。
3. 幂等。已经镜像过且 digest/大小都对得上的资产，连下载都跳过；
   定时任务每 6 小时跑一次不会重复传几百 MB。
4. 不静默覆盖。已存在但 digest 不同的资产默认只报警（::warning）并以退出码 1 结束，
   必须显式 --replace 才覆盖。历史版本被悄悄换掉比同步失败更糟。
5. 只镜像装得上的东西。默认只搬主 APK + 它的 .sha256；low 变体（+350MB）和
   source.zip（+700MB）要用 --include-low / --include-source 显式打开。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
UA = "dsha-release-mirror"
MANIFEST = "mirror-manifest.json"


def log(msg: str) -> None:
    print(msg, flush=True)


def api(method: str, path: str, token: str, data=None):
    """调用 GitHub REST API，返回 (status, payload)。4xx 不抛异常，交给调用方判断。"""
    url = path if path.startswith("http") else API + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", UA)
    req.add_header("Authorization", "Bearer " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"message": raw[:400].decode("utf-8", "replace")}


def download(url: str, dest: str, retries: int = 4) -> None:
    """用 curl 下载。

    用 curl 而不是 urllib：release 资产会 302 到 objects.githubusercontent.com，
    curl 跨主机时会自动丢掉 Authorization 头，不会把 token 泄露给 CDN。
    """
    cmd = [
        "curl", "-sSL", "--fail", "--retry", str(retries), "--retry-delay", "3",
        "--connect-timeout", "30", "--max-time", "2400", "-o", dest, url,
    ]
    subprocess.run(cmd, check=True)
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("下载结果为空: " + url)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def norm(value) -> str | None:
    """从 'sha256:abc…' 或 '.sha256 文件内容' 里抽出 64 位十六进制摘要。"""
    if not value:
        return None
    match = re.search(r"([0-9a-fA-F]{64})", str(value))
    return match.group(1).lower() if match else None


def is_sidecar(name: str) -> bool:
    return name.lower().endswith(".sha256")


def select_assets(assets, include_low: bool, include_source: bool):
    """默认只要主 APK 和它的 .sha256 旁车；low 变体和源码包需显式开启。"""
    chosen = []
    for asset in assets:
        name = asset.get("name", "")
        if asset.get("state") != "uploaded":
            continue
        lowered = name.lower()
        if "low.apk" in lowered and not include_low:
            continue
        if lowered.endswith(".zip") or lowered.endswith(".zip.sha256"):
            if include_source:
                chosen.append(asset)
            continue
        if lowered.endswith(".apk") or is_sidecar(name):
            chosen.append(asset)
    chosen.sort(key=lambda a: (1 if is_sidecar(a["name"]) else 0, a["name"]))
    return chosen


def upload_asset(repo: str, release_id: int, name: str, path: str, token: str):
    url = "{}/repos/{}/releases/{}/assets?name={}".format(UPLOADS, repo, release_id, urllib.parse.quote(name))
    cmd = [
        "curl", "-sS", "--fail-with-body", "-X", "POST",
        "-H", "Authorization: Bearer " + token,
        "-H", "Accept: application/vnd.github+json",
        "-H", "Content-Type: application/octet-stream",
        "-H", "User-Agent: " + UA,
        "--data-binary", "@" + path,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("上传失败 {}: {}".format(name, (result.stderr or result.stdout)[:400]))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("上传返回非 JSON: " + result.stdout[:400])


def pick_upstream_releases(token: str, upstream: str, limit: int, tag: str | None):
    status, releases = api("GET", "/repos/{}/releases?per_page=30".format(upstream), token)
    if status != 200 or not isinstance(releases, list):
        raise RuntimeError("读取上游 release 失败: {} {}".format(status, releases))
    usable = [r for r in releases if not r.get("draft")]
    if tag:
        matched = [r for r in usable if r.get("tag_name") == tag]
        if not matched:
            raise RuntimeError("上游不存在 tag: " + tag)
        return matched[:1]
    # 上游只要有正式版，就只跟正式版；全部是预发布时才跟进预发布
    stable = [r for r in usable if not r.get("prerelease")]
    return (stable or usable)[:limit]


def mirror_release(token: str, upstream: str, fork: str, release, args) -> list[dict]:
    tag = release["tag_name"]
    assets = select_assets(release.get("assets") or [], args.include_low, args.include_source)
    if not assets:
        log("[{}] 上游没有符合条件的资产，跳过".format(tag))
        return []
    if not any(a["name"].lower().endswith(".apk") for a in assets):
        raise RuntimeError("[{}] 选中资产里没有 APK，拒绝镜像".format(tag))

    # 上游索引：name -> digest / size / 下载地址
    upstream_index = {}
    for asset in assets:
        upstream_index[asset["name"]] = {
            "digest": norm(asset.get("digest")),
            "size": asset.get("size"),
            "url": asset["browser_download_url"],
        }

    status, fork_release = api("GET", "/repos/{}/releases/tags/{}".format(fork, tag), token)
    existing = {}
    if status == 200:
        existing = {a["name"]: a for a in (fork_release.get("assets") or [])}
        log("[{}] fork 已有该 Release（id={}，{} 个资产）".format(tag, fork_release["id"], len(existing)))
    elif status == 404:
        log("[{}] fork 还没有该 Release，将新建".format(tag))
    else:
        raise RuntimeError("查询 fork release 失败: {} {}".format(status, fork_release))

    def unchanged(name: str) -> bool:
        """已镜像且与上游记录一致——连下载都省了。"""
        current = existing.get(name)
        want = upstream_index[name]
        if not current or not want["digest"]:
            return False
        return norm(current.get("digest")) == want["digest"] and current.get("size") == want["size"]

    plan = []          # [{name, action, path?, old_id?}]
    conflicts = []
    with tempfile.TemporaryDirectory() as workdir:
        # ① 先拿旁车，旁车本身就是「上游对这个文件的官方声明」
        sidecars = {}
        for asset in assets:
            name = asset["name"]
            if not is_sidecar(name):
                continue
            if unchanged(name):
                log("[{}] {} 与上游记录一致，跳过".format(tag, name))
                continue
            if name in existing and not args.replace:
                conflicts.append(name)
                continue
            action = "replace" if name in existing else "upload"
            if args.dry_run:
                plan.append({"name": name, "action": action, "path": None,
                             "old_id": existing.get(name, {}).get("id")})
                continue
            path = os.path.join(workdir, name)
            download(upstream_index[name]["url"], path)
            claimed = upstream_index[name]["digest"]
            actual = sha256_file(path)
            if claimed and claimed != actual:
                raise RuntimeError("{} 旁车文件自身校验失败".format(name))
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                sidecars[name[: -len(".sha256")]] = norm(handle.read())
            log("    旁车 {} 读取完成".format(name))
            plan.append({"name": name, "action": action, "path": path,
                         "old_id": existing.get(name, {}).get("id")})

        # ② 主体文件：APK（或源码包）
        verified = {}
        for asset in assets:
            name = asset["name"]
            if is_sidecar(name):
                continue
            if unchanged(name):
                log("[{}] {} 与上游记录一致，跳过".format(tag, name))
                continue
            if name in existing and not args.replace:
                conflicts.append(name)
                continue
            action = "replace" if name in existing else "upload"
            if args.dry_run:
                # dry-run 不下载几百 MB：只按上游记录比对，够判断"会不会动"
                log("[{}] {} 需要{}（dry-run 跳过下载）".format(
                    tag, name, "覆盖" if action == "replace" else "上传"))
                plan.append({"name": name, "action": action, "path": None,
                             "old_id": existing.get(name, {}).get("id")})
                continue
            path = os.path.join(workdir, name)
            log("[{}] 下载 {} ({:.1f} MiB)".format(tag, name, upstream_index[name]["size"] / 1048576))
            download(upstream_index[name]["url"], path)
            actual = sha256_file(path)
            claimed = upstream_index[name]["digest"]
            sidecar = sidecars.get(name)
            if claimed and claimed != actual:
                raise RuntimeError("{} 与上游 digest 不符（可能下载被截断或被篡改）".format(name))
            if sidecar and sidecar != actual:
                raise RuntimeError("{} 与上游 .sha256 旁车不符".format(name))
            if not claimed and not sidecar:
                raise RuntimeError("{} 既无 digest 也无 .sha256，无法校验，拒绝镜像".format(name))
            log("    校验通过 sha256={}".format(actual))
            verified[name] = {"path": path, "sha256": actual, "size": upstream_index[name]["size"]}
            plan.append({"name": name, "action": action, "path": path,
                         "old_id": existing.get(name, {}).get("id")})

        if not verified:
            log("[{}] 本轮没有需要新传的主体资产".format(tag))

        # ③ 校验清单：只在缺失时补一次，保证重复运行结果稳定
        manifest = {
            "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "upstream": "{}/releases/tag/{}".format(upstream, tag),
            "upstream_release_id": release["id"],
            "upstream_published_at": release.get("published_at"),
            "tag": tag,
            "prerelease": bool(release.get("prerelease")),
            "note": "资产为上游原样搬运，未重新打包或签名；sha256 应与上游一致。",
            # 清单描述「这个 Release 里有哪些主产物」，本轮的已验证值优先，
            # 之前镜像过、本轮跳过的则沿用上游记录，避免清单只写了半截
            "assets": {
                name: {
                    "sha256": verified[name]["sha256"] if name in verified else item["digest"],
                    "size": item["size"],
                    "mirrored": name in existing and unchanged(name),
                }
                for name, item in upstream_index.items()
                if not is_sidecar(name)
            },
            "upstream_assets": {
                name: {"sha256": item["digest"], "size": item["size"]}
                for name, item in upstream_index.items()
                if item["digest"]
            },
        }
        manifest_path = os.path.join(workdir, MANIFEST)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        if MANIFEST not in existing and not args.dry_run:
            plan.append({"name": MANIFEST, "action": "upload", "path": manifest_path})
        elif MANIFEST not in existing:
            plan.append({"name": MANIFEST, "action": "upload", "path": None})

        if not plan and not conflicts:
            log("[{}] 无需变更".format(tag))
            return [{"tag": tag, "asset": "-", "action": "unchanged"}]

        if conflicts:
            log("::warning::[{}] 以下资产在 fork 已存在但摘要与上游不同（未加 --replace，保持原样）：{}".format(
                tag, ", ".join(conflicts)))

        if args.dry_run:
            for item in plan:
                log("[{}] dry-run 将执行 {} {}".format(tag, item["action"], item["name"]))
            return (
                [{"tag": tag, "asset": i["name"], "action": "dry-run:" + i["action"]} for i in plan]
                + [{"tag": tag, "asset": n, "action": "conflict"} for n in conflicts]
            )

        # ④ 落盘：先建 Release（若缺），再按 主体 → 旁车 → 清单 的顺序上传
        if status == 404:
            body = (release.get("body") or "").strip()
            prefix = (
                "> 本页由镜像工作流自动同步自上游 [DSH-APP/DSHA](https://github.com/DSH-APP/DSHA)"
                " 的 `{tag}`；资产未重新打包、未重新签名，sha256 与上游一致。\n"
                "> 同步时间 {now}。逐项校验值见附件 `mirror-manifest.json`。\n\n"
            ).format(tag=tag, now=manifest["synced_at"])
            created_status, created = api(
                "POST", "/repos/{}/releases".format(fork), token,
                {
                    "tag_name": tag,
                    "name": release.get("name") or tag,
                    "body": prefix + body,
                    "prerelease": bool(release.get("prerelease")),
                    "draft": False,
                },
            )
            if created_status not in (200, 201):
                raise RuntimeError("创建 Release 失败: {} {}".format(created_status, created))
            fork_release = created
            log("[{}] 已创建 Release id={}".format(tag, fork_release["id"]))

        order = {MANIFEST: 2}
        plan.sort(key=lambda i: (order.get(i["name"], 0), i["name"]))
        results = []
        for item in plan:
            if item["action"] == "replace":
                log("[{}] 删除旧资产 {}".format(tag, item["name"]))
                api("DELETE", "/repos/{}/releases/assets/{}".format(fork, item["old_id"]), token)
            log("[{}] 上传 {} ...".format(tag, item["name"]))
            uploaded = upload_asset(fork, fork_release["id"], item["name"], item["path"], token)
            got = norm(uploaded.get("digest")) if isinstance(uploaded, dict) else None
            results.append({"tag": tag, "asset": item["name"], "action": item["action"], "digest": got})

        for name in conflicts:
            results.append({"tag": tag, "asset": name, "action": "conflict"})
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description="镜像上游 DSHA release 到本仓库")
    parser.add_argument("--upstream", default=os.environ.get("UPSTREAM", "DSH-APP/DSHA"))
    parser.add_argument("--fork", default=os.environ.get("FORK") or os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--tag", default=os.environ.get("UPSTREAM_TAG") or None)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("LIMIT") or 1))
    parser.add_argument("--include-low", action="store_true",
                        default=str(os.environ.get("INCLUDE_LOW", "")).lower() == "true")
    parser.add_argument("--include-source", action="store_true",
                        default=str(os.environ.get("INCLUDE_SOURCE", "")).lower() == "true")
    parser.add_argument("--replace", action="store_true",
                        default=str(os.environ.get("REPLACE", "")).lower() == "true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.token:
        log("缺少 token（GH_TOKEN / GITHUB_TOKEN）")
        return 2
    if not args.fork or "/" not in args.fork:
        log("缺少 fork 仓库（owner/name），例如 Liaoyun666/DSHA")
        return 2

    targets = pick_upstream_releases(args.token, args.upstream, args.limit, args.tag)
    log("本轮检查上游 Release: " + ", ".join(r["tag_name"] for r in targets))

    summary: list[dict] = []
    for release in targets:
        summary.extend(mirror_release(args.token, args.upstream, args.fork, release, args))

    lines = ["## DSHA mirror sync", "", "| tag | asset | action |", "| --- | --- | --- |"]
    for row in summary:
        lines.append("| {} | {} | {} |".format(row.get("tag", ""), row.get("asset", "-"), row.get("action", "-")))
    if not summary:
        lines.append("| - | - | 无变更 |")
    table = "\n".join(lines)
    log("\n" + table)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(table + "\n")

    if any(row.get("action") == "conflict" for row in summary):
        log("存在摘要冲突且未覆盖：请人工确认后加 --replace 重跑")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
