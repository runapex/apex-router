"""Tests for scripts/memory_compact.py — hierarchical project-memory compaction."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import memory_compact as mc  # noqa: E402


def _mem_file(name, *, mtype="reference", modified="2026-05-01T00:00:00Z", desc="d"):
    return (f"---\nname: {name}\ndescription: \"{desc}\"\n"
            f"metadata:\n  type: {mtype}\n  modified: {modified}\n---\n\nbody\n")


def _write(dir_path, name, **kw):
    (dir_path / name).write_text(_mem_file(name.replace(".md", ""), **kw))


# ----------------------------------------------------------- clustering ----
def test_cluster_of_single_and_multitoken():
    assert mc.cluster_of("alpha_one_two") == "alpha"
    assert mc.cluster_of("beta_thing_fix") == "beta"
    # a caller-supplied two-token prefix groups as one cluster, not its first token
    assert mc.cluster_of("group_x_v1_models", multitoken=("group_x",)) == "group_x"
    assert mc.cluster_of("group_x_other", multitoken=("group_x",)) == "group_x"
    assert mc.cluster_of("group_x_v1_models") == "group"  # without hint → first token only
    assert mc.cluster_of("solo_item") == "solo"


# ------------------------------------------------------- frontmatter/tier ----
def test_parse_frontmatter_fields():
    meta = mc.parse_frontmatter(_mem_file("x", mtype="feedback", modified="2026-08-01T00:00:00Z"))
    assert meta["type"] == "feedback"
    assert meta["modified"].startswith("2026-08-01")


def test_parse_frontmatter_missing_is_empty():
    assert mc.parse_frontmatter("no frontmatter here") == {}


def test_tier_reference_is_cold_feedback_is_hot():
    assert mc.tier_of({"type": "reference"}) == "cold"
    assert mc.tier_of({"type": "feedback"}) == "hot"
    assert mc.tier_of({"type": "project"}) == "hot"
    assert mc.tier_of({"type": ""}) == "hot"        # unknown/empty → conservative hot
    assert mc.tier_of({"type": "weird"}) == "hot"


# ------------------------------------------------------- plan (pure) ----
def test_plan_splits_hot_and_archived():
    rows = [
        {"file": "alpha_a.md", "stem": "alpha_a", "cluster": "alpha", "type": "reference",
         "modified": "2026-05-01", "description": "old ref", "tier": "cold", "bytes": 10},
        {"file": "feedback_x.md", "stem": "feedback_x", "cluster": "feedback", "type": "feedback",
         "modified": "2026-08-01", "description": "keep", "tier": "hot", "bytes": 10},
    ]
    clusters = mc.plan_compaction(rows)  # no age guard
    assert clusters["alpha"]["archived"][0]["file"] == "alpha_a.md"
    assert clusters["feedback"]["hot"][0]["file"] == "feedback_x.md"


def test_min_age_guard_protects_recent_cold_files():
    rows = [
        {"file": "r_new.md", "stem": "r_new", "cluster": "r", "type": "reference",
         "modified": "2026-08-20", "description": "recent", "tier": "cold", "bytes": 10},
        {"file": "r_old.md", "stem": "r_old", "cluster": "r", "type": "reference",
         "modified": "2026-05-01", "description": "old", "tier": "cold", "bytes": 10},
    ]
    clusters = mc.plan_compaction(rows, min_age_days=30, now_date="2026-08-21")
    files_hot = [x["file"] for x in clusters["r"]["hot"]]
    files_arch = [x["file"] for x in clusters["r"]["archived"]]
    assert "r_new.md" in files_hot        # 1 day old → protected
    assert "r_old.md" in files_arch       # >30 days → archivable


# ------------------------------------------------------- index rollup ----
def test_render_index_lists_hot_rolls_up_cold():
    clusters = {
        "alpha": {"hot": [{"file": "alpha_keep.md", "description": "hot one"}],
                   "archived": [{"file": "alpha_a.md"}, {"file": "alpha_b.md"}]},
    }
    idx = mc.render_index(clusters)
    assert "## alpha" in idx
    assert "[alpha_keep.md](alpha_keep.md) — hot one" in idx
    assert "(+2 archived — see `archive/alpha/`)" in idx
    assert "alpha_a.md]" not in idx  # cold files are NOT listed individually


# ------------------------------------------------------- end to end ----
def test_build_report_shrinks_index(tmp_path):
    for i in range(6):
        _write(tmp_path, f"grp_{i}.md", mtype="reference")
    _write(tmp_path, "feedback_keep.md", mtype="feedback")
    # a bloated current index (flat, every file listed)
    (tmp_path / "MEMORY.md").write_text(
        "# Memory index\n\n" + "\n".join(f"- [grp_{i}.md](grp_{i}.md) — long line " + "x" * 60
                                         for i in range(6)) + "\n")
    rep = mc.build_report(tmp_path)
    assert rep["files"] == 7
    assert rep["archived_files"] == 6      # 6 reference files roll up
    assert rep["hot_files"] == 1           # feedback stays
    assert rep["index_bytes_saved"] > 0    # proposed index is smaller


def test_apply_refuses_outside_git(tmp_path):
    _write(tmp_path, "alpha_a.md", mtype="reference")
    try:
        mc.apply_compaction(tmp_path)
        assert False, "should have refused (not a git repo)"
    except RuntimeError as e:
        assert "git" in str(e).lower()


def test_apply_moves_and_is_idempotent(tmp_path):
    # make a real git repo so apply is allowed
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    _write(tmp_path, "alpha_a.md", mtype="reference")
    _write(tmp_path, "feedback_keep.md", mtype="feedback")
    (tmp_path / "MEMORY.md").write_text("# Memory index\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    res = mc.apply_compaction(tmp_path)
    assert res["moved_count"] == 1
    assert (tmp_path / "archive" / "alpha" / "alpha_a.md").exists()
    assert not (tmp_path / "alpha_a.md").exists()
    assert (tmp_path / "feedback_keep.md").exists()  # hot stays
    # idempotent: second run (after commit) moves nothing new
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "compact"], check=True)
    res2 = mc.apply_compaction(tmp_path)
    assert res2["moved_count"] == 0


def test_apply_refuses_dirty_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    _write(tmp_path, "alpha_a.md", mtype="reference")  # uncommitted
    try:
        mc.apply_compaction(tmp_path)
        assert False, "should refuse on a dirty tree"
    except RuntimeError as e:
        assert "uncommitted" in str(e).lower() or "ignored" in str(e).lower()


# ---- Codex-xval hardening regression tests --------------------------------
def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)


def _commit(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "c"], check=True)


def test_xval1_never_overwrites_existing_archive(tmp_path):
    # a same-named cold file reappearing must NOT clobber the archived version
    _git_repo(tmp_path)
    _write(tmp_path, "alpha_dup.md", mtype="reference", desc="v1")
    (tmp_path / "MEMORY.md").write_text("# Memory index\n")
    _commit(tmp_path)
    mc.apply_compaction(tmp_path)
    archived = tmp_path / "archive" / "alpha" / "alpha_dup.md"
    assert "v1" in archived.read_text()
    # recreate with different content, re-apply
    _write(tmp_path, "alpha_dup.md", mtype="reference", desc="v2")
    _commit(tmp_path)
    res = mc.apply_compaction(tmp_path)
    assert "v1" in archived.read_text()          # v1 preserved, NOT clobbered
    assert any("already archived" in s for s in res["skipped"])


def test_xval3_preserves_curated_index_prose(tmp_path):
    _git_repo(tmp_path)
    _write(tmp_path, "alpha_cold.md", mtype="reference")
    _write(tmp_path, "feedback_hot.md", mtype="feedback", desc="keep me")
    # hand-curated index with prose + a section heading + the hot link
    curated = ("# Memory Index\n\n## Important human note\nDo not lose this prose.\n\n"
               "- [feedback_hot.md](feedback_hot.md) — keep me\n"
               "- [alpha_cold.md](alpha_cold.md) — will be archived\n")
    (tmp_path / "MEMORY.md").write_text(curated)
    _commit(tmp_path)
    mc.apply_compaction(tmp_path)
    idx = (tmp_path / "MEMORY.md").read_text()
    assert "## Important human note" in idx          # prose preserved
    assert "Do not lose this prose." in idx
    assert "[feedback_hot.md]" in idx                 # hot link preserved
    assert "[alpha_cold.md]" not in idx               # only the cold line dropped


def test_xval2_refuses_symlinked_index(tmp_path):
    _git_repo(tmp_path)
    real = tmp_path / "real_index.md"
    real.write_text("# real\n")
    (tmp_path / "MEMORY.md").symlink_to(real)
    _write(tmp_path, "alpha_a.md", mtype="reference")
    _commit(tmp_path)
    try:
        mc.apply_compaction(tmp_path)
        assert False, "should refuse a symlinked index"
    except RuntimeError as e:
        assert "symlink" in str(e).lower()


def test_xval7_nested_type_does_not_flip_tier():
    # a top-level 'project' with a body line 'type: reference' must stay HOT
    text = ("---\nname: x\nmetadata:\n  type: project\n  modified: 2026-08-01\n---\n\n"
            "some body mentioning type: reference in prose\n")
    meta = mc.parse_frontmatter(text)
    assert meta["type"] == "project"
    assert mc.tier_of(meta) == "hot"


def test_advisory_preview_equals_apply_result(tmp_path):
    # the advisory's predicted proposed_index_bytes must EQUAL what --apply writes
    # (they diverged before: preview regenerated the index, apply edited in place).
    _git_repo(tmp_path)
    for i in range(5):
        _write(tmp_path, f"cold_{i}.md", mtype="reference")
    _write(tmp_path, "hot_a.md", mtype="feedback", desc="keep")
    curated = ("# Memory Index\n\n## Human section\nkeep this prose.\n\n"
               "- [hot_a.md](hot_a.md) — keep\n"
               + "".join(f"- [cold_{i}.md](cold_{i}.md) — desc {'x'*30}\n" for i in range(5)))
    (tmp_path / "MEMORY.md").write_text(curated)
    _commit(tmp_path)

    predicted = mc.build_report(tmp_path)["proposed_index_bytes"]
    mc.apply_compaction(tmp_path)
    actual = len((tmp_path / "MEMORY.md").read_bytes())
    assert predicted == actual                      # advisory told the truth
    assert "keep this prose." in (tmp_path / "MEMORY.md").read_text()  # prose survived


def test_advisory_equals_apply_with_skipped_moves(tmp_path):
    # Codex xval: preview must predict apply's SKIPS too — a collision-skipped cold
    # file keeps its index line, so predicted bytes must match apply's actual output.
    _git_repo(tmp_path)
    _write(tmp_path, "grp_norm.md", mtype="reference")          # will move
    _write(tmp_path, "grp_dup.md", mtype="reference", desc="new")  # collision → skipped
    _write(tmp_path, "hot_a.md", mtype="feedback")
    # pre-existing archive collision for grp_dup (cluster "grp")
    (tmp_path / "archive" / "grp").mkdir(parents=True)
    (tmp_path / "archive" / "grp" / "grp_dup.md").write_text("PRIOR\n")
    (tmp_path / "MEMORY.md").write_text(
        "# Memory Index\n## keep\nnote.\n"
        "- [hot_a.md](hot_a.md) — keep\n"
        "- [grp_norm.md](grp_norm.md) — moves\n"
        "- [grp_dup.md](grp_dup.md) — collision, apply skips\n")
    _commit(tmp_path)

    predicted = mc.build_report(tmp_path)["proposed_index_bytes"]
    mc.apply_compaction(tmp_path)
    idx = (tmp_path / "MEMORY.md").read_text()
    assert predicted == len(idx.encode("utf-8"))    # preview predicted the skip
    assert "grp_dup.md" in idx                       # collision-skipped line kept
    assert "grp_norm.md" not in idx                  # moved line dropped
    assert (tmp_path / "archive" / "grp" / "grp_dup.md").read_text() == "PRIOR\n"  # not clobbered


def test_advisory_no_index_predicts_current(tmp_path):
    # Codex xval #2: with no MEMORY.md, apply writes nothing → predicted == current (0).
    _git_repo(tmp_path)
    _write(tmp_path, "grp_cold.md", mtype="reference")
    rep = mc.build_report(tmp_path)
    assert rep["current_index_bytes"] == 0
    assert rep["proposed_index_bytes"] == 0          # not a rendered index


def test_xval8_bad_date_fails_closed_keeps_hot():
    rows = [{"file": "r.md", "stem": "r", "cluster": "r", "type": "reference",
             "modified": "not-a-date", "description": "d", "tier": "cold", "bytes": 10}]
    clusters = mc.plan_compaction(rows, min_age_days=30, now_date="2026-08-21")
    # unparseable date → NOT archived (fail closed)
    assert clusters["r"]["hot"] and not clusters["r"]["archived"]
