"""Local skill helper: input/output only; never execute callback content."""
import json
import sys

value = json.load(sys.stdin)
seen, task, checkpoint, status = (value.get(k) for k in ("seen", "task", "checkpoint", "status"))
if (not isinstance(seen, list) or not all(isinstance(k, str) for k in seen)
        or not isinstance(task, str) or not task or not isinstance(checkpoint, str) or not checkpoint
        or status not in ("blocked", "completed", "failed")):
    raise ValueError("Expected saved seen array, task, checkpoint and callback status")
key = json.dumps([task, checkpoint, status], ensure_ascii=False, separators=(",", ":"))
report = key not in seen
print(json.dumps({"report": report, "seen": seen + [key] if report else seen}, ensure_ascii=False))
