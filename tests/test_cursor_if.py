import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core

# steps = [ A, IF(then=[B, C], else=[Z]), E ]
A = {"type": "tap", "label": "A"}
B = {"type": "tap", "label": "B"}
C = {"type": "tap", "label": "C"}
Z = {"type": "tap", "label": "Z"}
E = {"type": "tap", "label": "E"}
IF = {"type": "if", "label": "分岐", "condition": {"kind": "image_found"}, "then": [B, C], "else": [Z]}
root = [A, IF, E]

cur = core.Cursor(root)

# 1. 最初はA
assert cur.current() is A
cur.advance()

# 2. 次はIF自体
assert cur.current() is IF
assert cur.depth() == 1

# 3. thenへ入る(enter_block) -> 親(root)のIF位置は1つ進み、子(then)へ潜る
cur.enter_block(IF["then"])
assert cur.depth() == 2
assert cur.current() is B

# 4. Bで待機中のスキップ探索候補は「thenの中の残り兄弟」だけ(=C)。
#    else(Z)やIFの後(E)には絶対に届かないことを確認
candidates = cur.siblings_ahead(10)  # 大きな範囲を指定しても届かないことを確認
assert candidates == [C], candidates
assert Z not in candidates
assert E not in candidates
print("containment OK: siblings_ahead from inside then =", [n["label"] for n in candidates])

# 5. is_at_start(): enter_blockでは_movedは変化しないはず(Aでadvance済みなのでTrue)
assert cur.is_at_start() is False  # Aの後にadvance済みなのでTrue(=moved)

# 6. Bを実行 → advance
cur.advance()
assert cur.current() is C
# ここでのsiblings_aheadは空(Cが最後)
assert cur.siblings_ahead(10) == []

# 7. Cを実行 → advance。thenブロックが尽きるのでcurrent()が自動popしてIFの次=Eへ
cur.advance()
assert cur.depth() == 2  # advance直後はまだthenフレームのままindex=2(len=2)
node = cur.current()  # ここで自動popが起きる
assert node is E, node
assert cur.depth() == 1
print("block exit OK: after then exhausted, current() ==", node["label"], "depth ==", cur.depth())

print("=== Cursor if/else mechanics: ALL CHECKS PASSED ===")

# --- else側のケースも同様に確認 ---
cur2 = core.Cursor([A, IF, E])
cur2.advance()  # A実行済み想定
assert cur2.current() is IF
cur2.enter_block(IF["else"])
assert cur2.depth() == 2
assert cur2.current() is Z
# elseの中からのスキップ探索候補も、else内の残り兄弟のみ(この場合は空)
assert cur2.siblings_ahead(10) == []
cur2.advance()
node2 = cur2.current()
assert node2 is E, node2
assert cur2.depth() == 1
print("else-branch containment + exit OK")

# --- 3階層ネストのdepth確認 ---
INNER = {"type": "if", "label": "内側", "condition": {"kind": "image_found"},
         "then": [{"type": "tap", "label": "深い"}], "else": []}
OUTER_THEN = [INNER]
cur3 = core.Cursor([{"type": "if", "label": "外側", "condition": {"kind": "image_found"},
                      "then": OUTER_THEN, "else": []}])
assert cur3.depth() == 1
outer_if = cur3.current()
cur3.enter_block(outer_if["then"])
assert cur3.depth() == 2
inner_if = cur3.current()
assert inner_if is INNER
cur3.enter_block(inner_if["then"])
assert cur3.depth() == 3
deep_node = cur3.current()
assert deep_node["label"] == "深い"
print("3-level nesting depth() ==", cur3.depth())

print("=== ALL TESTS PASSED ===")
