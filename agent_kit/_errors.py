"""错误事件的诊断辅助 —— 内部模块,不对外暴露。

主要用途:把 `BaseExceptionGroup` 解开,让 error event 的 `message` 字段
显示真实根因而不是 "unhandled errors in a TaskGroup (1 sub-exception)"。

触发场景:MCP `streamablehttp_client` / `stdio_client` 等 async 上下文用
`anyio.create_task_group` 包了错误,异常向上传时变成 ExceptionGroup。SDK
之前 `str(exc)` 拿到无意义文本,这里把它剥到叶子节点。

`traceback` 字段保持完整链(`traceback.format_exception` 在 Python 3.11+
对 ExceptionGroup 会自然展开),所以 debug 时根因 + 上下文都在 event 里。
"""

from __future__ import annotations


def unwrap_to_leaf(exc: BaseException) -> BaseException:
    """把 BaseExceptionGroup 沿 `.exceptions[0]` 走到第一个叶子节点。

    非 group 直接返回。空 group(理论不该发生)也直接返回。
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


__all__ = ["unwrap_to_leaf"]
