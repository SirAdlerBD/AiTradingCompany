"""Run logging with two levels. `info` always prints (what the systemd timer sees in
journalctl); `detail` prints only under `desk run --verbose`. Callable for the
older `log(...)` call sites, which map to info."""
from __future__ import annotations


class Log:
    def __init__(self, verbose: bool = False, out=print, quiet: bool = False):
        self.verbose = verbose
        self.out = out
        self.quiet = quiet
        self.cost_usd = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0

    def __call__(self, msg: str) -> None:
        self.info(msg)

    def info(self, msg: str) -> None:
        if not self.quiet:
            self.out(msg)

    def detail(self, msg: str) -> None:
        if self.verbose and not self.quiet:
            self.out(msg)

    def block(self, title: str, lines: list[str]) -> None:
        """A titled, indented block; verbose only."""
        if not self.verbose or self.quiet:
            return
        self.out(f"  ┌─ {title}")
        for ln in lines:
            for i, part in enumerate(str(ln).splitlines() or [""]):
                self.out(f"  │ {part}" if i == 0 else f"  │   {part}")
        self.out("  └─")

    def cost(self, role: str, model: str, tokens_in, tokens_out, cost_usd, latency_ms, attempt: int, ok: bool) -> None:
        """Per-call usage line (verbose) and the running total (printed by the run at the end)."""
        self.calls += 1
        self.tokens_in += tokens_in or 0
        self.tokens_out += tokens_out or 0
        self.cost_usd += cost_usd or 0.0
        cost = "n/a" if cost_usd is None else f"${cost_usd:.4f}"
        self.detail(f"  [usage] {role} ({model}) attempt {attempt} {'ok' if ok else 'rejected'}: "
                    f"in={tokens_in if tokens_in is not None else '?'} out={tokens_out if tokens_out is not None else '?'} "
                    f"cost {cost} latency {latency_ms if latency_ms is not None else '?'}ms")

    def summary(self) -> str:
        return (f"model calls: {self.calls}, tokens in {self.tokens_in} / out {self.tokens_out}, "
                f"estimated cost ${self.cost_usd:.4f} (from per-role prices in config)")


def as_log(log) -> Log:
    """Accept a Log, a plain callable, or None."""
    if isinstance(log, Log):
        return log
    if log is None:
        return Log()
    return Log(out=lambda m: log(m))
