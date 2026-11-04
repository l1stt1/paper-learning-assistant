"""Model calls for the reading assistant; credentials never enter saved settings."""
from __future__ import annotations

import base64
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULTS = {
    "backend": "codex",
    "base_url": "https://api.openai.com/v1",
    "model": "",
    "key_env": "OPENAI_API_KEY",
    "reasoning_effort": "",
    "max_output_tokens": 16384,
}
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,119}\Z")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ModelBackendError(RuntimeError):
    """A reader-facing error with no provider response body or authentication data."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential to a redirect target.
        return None


def _valid_model(value):
    return isinstance(value, str) and bool(MODEL_NAME.fullmatch(value)) and not value.lower().startswith("sk-")


def _validated_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("模型设置包含不支持的字段；这里只接受模型参数和凭据环境变量名，不接受密钥。")
    result = {**DEFAULTS, **value}
    if not isinstance(result["backend"], str) or result["backend"] not in {"codex", "openai"}:
        raise ValueError("请选择 Codex CLI 或 OpenAI Responses API。")
    url = result["base_url"]
    if not isinstance(url, str) or len(url) > 500 or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url:
        raise ValueError("API 地址格式无效。")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        raise ValueError("API 地址格式无效。") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "sk-" in url.lower()):
        raise ValueError("API 地址必须使用 HTTPS，且不能包含账号、密码、密钥、查询参数或片段。")
    result["base_url"] = url.rstrip("/")
    if not isinstance(result["key_env"], str) or not ENV_NAME.fullmatch(result["key_env"]):
        raise ValueError("请输入凭据环境变量的名称，不要输入密钥值。")
    if result["model"] != "" and not _valid_model(result["model"]):
        raise ValueError("模型名称格式无效；不要在这里输入密钥。")
    if result["backend"] == "openai" and not result["model"]:
        raise ValueError("使用 API 前请明确填写模型名称。")
    if not isinstance(result["reasoning_effort"], str) or result["reasoning_effort"] not in {"", "none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("推理强度设置无效；可留空以使用模型默认值。")
    limit = result["max_output_tokens"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 256 <= limit <= 131072:
        raise ValueError("单次输出上限应为 256 至 131072 之间的整数，并须符合所选模型限制。")
    return result


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _usage_tokens(value):
    result = {}
    if isinstance(value, dict):
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10**12:
                result[key] = count
    return result


class ModelBackend:
    def __init__(self, data_root, codex_provider):
        self.data_root = Path(data_root)
        self.settings_path = self.data_root / "model-settings.json"
        self.review_path = self.data_root / "model-settings-review.json"
        self.usage_path = self.data_root / "model-usage.jsonl"
        self.codex_provider = codex_provider
        self.lock = threading.RLock()
        self._settings = dict(DEFAULTS)
        self._settings_error = ""
        self._usage_warning = ""
        self._requires_review = False
        if self.review_path.exists() or self.review_path.is_symlink():
            try:
                review = json.loads(self.review_path.read_text(encoding="utf-8"))
                self._requires_review = (not isinstance(review, dict) or set(review) != {"requires_review"}
                                         or review["requires_review"] is not False)
            except (OSError, ValueError, TypeError):
                self._requires_review = True
        if self.settings_path.exists():
            try:
                self._settings = _validated_settings(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                self._settings_error = "已保存的模型设置无法读取，请重新检查并保存；未自动切换模型后端。"

    def settings(self):
        with self.lock:
            return dict(self._settings)

    def configure(self, value):
        with self.lock:
            if not isinstance(value, dict):
                raise ValueError("模型设置应为对象。")
            settings = _validated_settings({**self._settings, **value})
            self.data_root.mkdir(parents=True, exist_ok=True)
            pending = self.settings_path.with_suffix(".json.pending")
            _write_json(pending, settings)
            os.replace(pending, self.settings_path)
            review_pending = self.review_path.with_suffix(".json.pending")
            _write_json(review_pending, {"requires_review": False})
            os.replace(review_pending, self.review_path)
            self._settings = settings
            self._settings_error = ""
            self._requires_review = False
        return self.status()

    def _readiness(self, settings):
        if self._requires_review:
            return False, "来自备份的模型设置需在模型设置中核对服务地址、模型和凭据变量后保存。"
        if self._settings_error:
            return False, self._settings_error
        if settings["backend"] == "codex":
            if self.codex_provider():
                return True, "使用本机 Codex CLI 的现有登录和模型配置。"
            return False, "未发现可用 Codex CLI；可检查现有安装，或配置模型 API。"
        if not settings["model"]:
            return False, "请填写 API 模型名称。"
        if not os.environ.get(settings["key_env"], "").strip():
            return False, "服务进程尚未读取到指定的凭据环境变量；配置环境变量后需重新启动阅读助手。"
        return True, "已在服务进程中读取到 API 凭据；尚未因此验证网络连接、模型权限或余额。"

    def available(self):
        with self.lock:
            return self._readiness(self._settings)[0]

    def status(self):
        with self.lock:
            settings = dict(self._settings)
            available, message = self._readiness(settings)
            return {
                "settings": settings,
                "available": available,
                "message": message,
                "label": "Codex CLI" if settings["backend"] == "codex" else "API · " + settings["model"],
                "usage": self._usage_summary(),
            }

    def _usage_summary(self):
        summary = {"calls": 0, "successful_calls": 0, "failed_calls": 0, "api_calls": 0,
                   "token_usage_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                   "last_call": None, "warning": self._usage_warning}
        if not self.usage_path.exists():
            return summary
        try:
            with self.usage_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        summary["warning"] = "部分用量记录无法读取；显示的统计可能不完整。"
                        continue
                    if not isinstance(record, dict) or record.get("backend") not in {"codex", "openai"}:
                        continue
                    summary["calls"] += 1
                    summary["successful_calls" if record.get("status") == "succeeded" else "failed_calls"] += 1
                    summary["api_calls"] += record["backend"] == "openai"
                    tokens = _usage_tokens(record)
                    if tokens:
                        summary["token_usage_calls"] += 1
                        for key, count in tokens.items():
                            summary[key] += count
                    # Expose only the fields this module writes, even if a file was manually changed.
                    summary["last_call"] = {
                        "backend": record["backend"],
                        "requested_model": record.get("requested_model") if _valid_model(record.get("requested_model")) else "",
                        "returned_model": record.get("returned_model") if _valid_model(record.get("returned_model")) else "",
                        "status": "succeeded" if record.get("status") == "succeeded" else "failed",
                        **tokens,
                    }
        except (OSError, UnicodeError):
            summary["warning"] = "暂时无法读取用量记录；不影响已保存的模型结果。"
        return summary

    def _record_usage(self, run_dir, record):
        # Usage bookkeeping must never invalidate a successfully preserved response.
        with self.lock:
            try:
                _write_json(run_dir / "model-call.json", record)
            except OSError:
                self._usage_warning = "部分调用统计未能写入；已成功保存的回答仍可继续使用。"
            try:
                self.data_root.mkdir(parents=True, exist_ok=True)
                with self.usage_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                self._usage_warning = "部分调用统计未能写入；已成功保存的回答仍可继续使用。"

    def generate(self, schema, prompt, images, run_dir, *, output_file="answer.json", schema_file="answer.schema.json", timeout=600, kind="question"):
        settings = self.settings()
        available, message = self._readiness(settings)
        if not available:
            raise ModelBackendError("not_configured", message)
        run_dir = Path(run_dir)
        for name in (output_file, schema_file):
            if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
                raise ValueError("模型输出文件名无效。")
        run_dir.mkdir(parents=True, exist_ok=True)
        schema_path, output_path = run_dir / schema_file, run_dir / output_file
        _write_json(schema_path, schema)
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "backend": settings["backend"],
                  "requested_model": settings["model"] if settings["backend"] == "openai" else "",
                  "returned_model": "", "kind": kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,40}", kind) else "generation",
                  "status": "failed", "error_code": "", "elapsed_seconds": 0}
        started = time.monotonic()
        try:
            if settings["backend"] == "codex":
                result = self._call_cli(prompt, images, run_dir, schema_path, output_path, timeout)
            else:
                result = self._call_api(settings, schema, prompt, images, timeout, record)
                _write_json(output_path, result)
            record["status"] = "succeeded"
            return result
        except ModelBackendError as exc:
            record["error_code"] = exc.code
            raise
        except subprocess.TimeoutExpired:
            record["error_code"] = "timeout"
            raise
        except OSError:
            record["error_code"] = "local_io"
            raise ModelBackendError("local_io", "模型组件无法启动或结果无法写入，请检查本地运行环境；未自动重试。") from None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self._record_usage(run_dir, record)

    def _call_cli(self, prompt, images, run_dir, schema_path, output_path, timeout):
        command = [self.codex_provider(), "--ask-for-approval", "never", "exec", "--ephemeral", "--sandbox", "read-only",
                   "--color", "never", "--skip-git-repo-check", "--output-schema", str(schema_path),
                   "--output-last-message", str(output_path), "-C", str(run_dir)]
        for image in images:
            command += ["--image", str(image)]
        command.append("-")
        completed = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace",
                                   capture_output=True, timeout=timeout,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode or not output_path.is_file():
            raise ModelBackendError("cli_failed", "Codex CLI 未完成生成，请检查现有登录与可用额度；未自动重试。")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ModelBackendError("invalid_json", "模型没有返回完整的 JSON 结果；原有记录保留。") from None
        if not isinstance(result, dict):
            raise ModelBackendError("invalid_json", "模型没有返回 JSON 对象；原有记录保留。")
        return result

    def _call_api(self, settings, schema, prompt, images, timeout, record):
        credential = os.environ.get(settings["key_env"], "")
        if not credential or any(char.isspace() or ord(char) < 32 for char in credential):
            raise ModelBackendError("not_configured", "服务进程尚未读取到指定的 API 凭据环境变量。")
        content = [{"type": "input_text", "text": prompt}]
        for image in images:
            encoded = base64.b64encode(Path(image).read_bytes()).decode("ascii")
            content.append({"type": "input_image", "image_url": "data:image/png;base64," + encoded})
        body = {"model": settings["model"], "input": [{"role": "user", "content": content}],
                "text": {"format": {"type": "json_schema", "name": "learning_result", "strict": True, "schema": schema}},
                "store": False, "max_output_tokens": settings["max_output_tokens"]}
        if settings["reasoning_effort"]:
            body["reasoning"] = {"effort": settings["reasoning_effort"]}
        request = Request(settings["base_url"] + "/responses", data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + credential}, method="POST")
        try:
            with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            code = exc.code if isinstance(exc.code, int) and 100 <= exc.code <= 599 else 500
            explanations = {401: "凭据无效或已失效", 403: "此账户没有相应访问权限", 404: "服务地址或模型不可用",
                            429: "请求受限或 API 可用额度不足"}
            detail = explanations.get(code, "服务未能完成请求")
            raise ModelBackendError("http_" + str(code), f"模型 API 请求失败（HTTP {code}）：{detail}。未自动重试。") from None
        except (TimeoutError, subprocess.TimeoutExpired):
            raise ModelBackendError("timeout", "模型 API 超过约定等待时间；未自动重试，请稍后手动继续。") from None
        except (URLError, HTTPException, ValueError):
            raise ModelBackendError("network", "暂时无法连接模型 API，请检查服务地址和网络；未自动重试。") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelBackendError("response_too_large", "模型 API 返回内容超过可读取范围；未自动重试。")
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ModelBackendError("invalid_response", "模型 API 返回的响应格式无法读取；未自动重试。") from None
        if not isinstance(response, dict):
            raise ModelBackendError("invalid_response", "模型 API 返回的响应格式不完整；未自动重试。")
        if credential in json.dumps(response, ensure_ascii=False):
            raise ModelBackendError("credential_echo", "模型服务返回了不应包含的认证内容；结果未保存，请检查该服务。")
        record.update(_usage_tokens(response.get("usage")))
        if _valid_model(response.get("model")) and response["model"] != credential:
            record["returned_model"] = response["model"]
        if response.get("status") != "completed" or response.get("error"):
            raise ModelBackendError("incomplete", "模型 API 未完整完成生成；请检查输出上限或服务状态后手动继续。")
        pieces = []
        for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if item.get("status", "completed") != "completed":
                raise ModelBackendError("incomplete", "模型 API 返回的回答尚未完成；未自动重试。")
            for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise ModelBackendError("refusal", "模型未能回答这次请求；原有记录保留，未自动重试。")
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
        if not pieces:
            raise ModelBackendError("empty_output", "模型 API 没有返回可用的文字回答；未自动重试。")
        try:
            result = json.loads("".join(pieces))
        except ValueError:
            raise ModelBackendError("invalid_json", "模型 API 没有返回完整的 JSON 结果；原有记录保留。") from None
        if not isinstance(result, dict):
            raise ModelBackendError("invalid_json", "模型 API 没有返回 JSON 对象；原有记录保留。")
        if credential in json.dumps(result, ensure_ascii=False):
            raise ModelBackendError("credential_echo", "模型服务返回了不应包含的认证内容；结果未保存，请检查该服务。")
        return result
