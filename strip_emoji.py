"""
Strip emoji characters from string literals only, leaving code structure intact.
Uses tokenize to find STRING tokens and replaces emoji within them.
"""
import re
import tokenize
import io
import sys

# Only the common UI emoji actually used in these files
EMOJI_RE = re.compile(
    "["
    "\u23F3"          # hourglass flowing sand ⏳
    "\u2705"          # white check mark ✅
    "\u274C"          # cross mark ❌
    "\U0001F4CB"      # clipboard 📋
    "\U0001F50D"      # magnifying glass 🔍
    "\u26A0\uFE0F?"   # warning ⚠️
    "\u26A0"          # warning bare
    "\U0001F64F"      # folded hands 🙏
    "\U0001F4DD"      # memo 📝
    "\U0001F61E"      # disappointed 😞
    "\U0001F64C"      # raising hands 🙌
    "\U0001F389"      # party popper 🎉
    "\U0001F4A1"      # bulb 💡
    "\U0001F527"      # wrench 🔧
    "\U0001F44B"      # waving hand 👋
    "\U0001F680"      # rocket 🚀
    "\U0001F4AC"      # speech bubble 💬
    "\U0001F504"      # arrows 🔄
    "\u26A1"          # lightning ⚡
    "\U0001F6D1"      # stop sign 🛑
    "\u2728"          # sparkles ✨
    "\U0001F440"      # eyes 👀
    "\U0001F4CA"      # bar chart 📊
    "\U0001F5D1"      # wastebasket 🗑
    "\U0001F914"      # thinking 🤔
    "\U0001F4B0"      # money bag 💰
    "\U0001F3AF"      # direct hit 🎯
    "\U0001F510"      # locked with key 🔐
    "\U0001F4CC"      # pushpin 📌
    "\U0001F3F7"      # label 🏷
    "\U0001F9EA"      # test tube 🧪
    "\U0001F310"      # globe 🌐
    "\U0001F464"      # bust 👤
    "\U0001F4B3"      # credit card 💳
    "\U0001F511"      # key 🔑
    "\U0001F4C5"      # calendar 📅
    "\u23F0"          # alarm clock ⏰
    "\U0001F551"      # clock 🕐
    "\U0001F5D3"      # spiral calendar 🗓
    "\U0001F4C8"      # chart up 📈
    "\U0001F4C9"      # chart down 📉
    "\U0001F512"      # locked 🔒
    "\U0001F513"      # unlocked 🔓
    "\uFE0F"          # variation selector (cleanup)
    "]",
    flags=re.UNICODE,
)

def strip_emoji_from_file(path: str) -> bool:
    with open(path, encoding="utf-8") as f:
        source = f.read()

    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    result = []
    changed = False
    prev_end = 0

    for tok_type, tok_string, tok_start, tok_end, tok_line in tokens:
        start_offset = _pos_to_offset(source, tok_start)
        end_offset = _pos_to_offset(source, tok_end)
        result.append(source[prev_end:start_offset])
        if tok_type == tokenize.STRING:
            new_string = EMOJI_RE.sub("", tok_string)
            # Clean up leading/trailing spaces inside quotes introduced by removal
            new_string = re.sub(r'(["\'`]) ', r'\1', new_string)
            new_string = re.sub(r' (["\'`])', r'\1', new_string)
            if new_string != tok_string:
                changed = True
            result.append(new_string)
        else:
            result.append(tok_string)
        prev_end = end_offset

    result.append(source[prev_end:])
    new_source = "".join(result)

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_source)
    return changed


def _pos_to_offset(source: str, pos: tuple) -> int:
    line, col = pos
    lines = source.splitlines(keepends=True)
    return sum(len(lines[i]) for i in range(line - 1)) + col


if __name__ == "__main__":
    files = sys.argv[1:] or ["bot.py", "slack_client.py", "workflow_executor.py", "feedback_store.py"]
    for f in files:
        changed = strip_emoji_from_file(f)
        print(f"{f}: {'cleaned' if changed else 'no change'}")
