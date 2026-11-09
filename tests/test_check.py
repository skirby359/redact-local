"""check, where and adopt: find what a run left in the output without printing it, and fix
names.txt without anyone typing a name. Synthetic data only."""

from __future__ import annotations

from conftest import CLIENT, CONFIG, LETTER, NAMES_TXT, assert_no_secrets, cli, out_dir, run

from redact_local import check, names


def _setup(matter, names_txt: str | None = None):
    if names_txt is not None:
        (matter / "_redact" / "names.txt").write_text(names_txt, encoding="utf-8")
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)


def _leak(matter, extra: str) -> None:
    """Simulate a miss: text a detection bug would have left in the output."""
    p = next(out_dir(matter).rglob("*.txt"))
    p.write_text(p.read_text(encoding="utf-8") + extra, encoding="utf-8")


def test_check_finds_known_values_neighbours_and_shapes(matter):
    _setup(matter)
    before = check.run_check(matter, CONFIG)
    _leak(matter, f'\nSigned: {CLIENT}\nFrom: "[CLIENT], Zelphine (Z.)" <[CLIENT-EMAIL]>\nCall 206-555-0199 today.\n')
    res = check.run_check(matter, CONFIG)
    known = [f for f in res.of("known") if f.placeholder == "CLIENT" and f.value == CLIENT]
    assert known and known[0].names_line == 2
    nb = [f for f in res.of("neighbour") if f.value == "Zelphine"]
    assert nb and nb[0].placeholder == "CLIENT" and nb[0].names_line == 2
    assert any(f.category == "PHONE" for f in res.of("shape"))
    assert len(res.findings) > len(before.findings)
    # The text is only in the private report; check.json carries what adopt needs.
    assert "Zelphine" in res.report.read_text(encoding="utf-8")
    assert check.load_findings(matter / "_redact")[nb[0].id]["value"] == "Zelphine"


def test_adopt_adds_the_word_to_the_persons_line(matter, tmp_path):
    _setup(matter)
    _leak(matter, '\nFrom: "[CLIENT], Zelphine (Z.)"\n')
    nb = next(f for f in check.run_check(matter, CONFIG).of("neighbour") if f.value == "Zelphine")
    proc = cli("adopt", str(matter), nb.id, cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "1 spelling added" in proc.stdout and "Changed line(s): 2" in proc.stdout
    entry = next(e for e in names.parse(matter / "_redact" / "names.txt").entries if e.role == "CLIENT")
    assert [a.text for a in entry.aliases][-1] == "Zelphine" and entry.aliases[0].text == CLIENT
    # The next run redacts it.
    (matter / "letter.txt").write_text(LETTER + "\nRegards, Zelphine\n", encoding="utf-8")
    run(matter)
    assert "zelphine" not in next(out_dir(matter).rglob("*.txt")).read_text(encoding="utf-8").lower()


def test_adopt_activates_a_commented_suggestion(matter, tmp_path):
    _setup(matter, NAMES_TXT + "# Robert Fakename =>                       # [PERSON-7], 1 mention in 1 file\n")
    _leak(matter, "\nRobert Fakename was cc'd.\n")
    res = check.run_check(matter, CONFIG)
    f = next(f for f in res.of("known") if f.value == "Robert Fakename")
    assert f.names_line == 4
    assert cli("adopt", str(matter), f.id, cwd=tmp_path).returncode == 0
    line = (matter / "_redact" / "names.txt").read_text(encoding="utf-8").splitlines()[3]
    assert not line.startswith("#") and "[PERSON-7]" in line


def test_adopt_refuses_a_finding_without_a_person(matter, tmp_path):
    _setup(matter)
    _leak(matter, "\nCall 206-555-0199 today.\n")
    shape = next(f for f in check.run_check(matter, CONFIG).of("shape") if f.category == "PHONE")
    proc = cli("adopt", str(matter), shape.id, cwd=tmp_path)
    assert proc.returncode == 2 and "isn't tied to a person" in proc.stderr


def test_check_lints_names_txt(matter):
    _setup(matter, NAMES_TXT + "Q. Fakerton => CLIENT\n# Robert Fakename => OTHER\n")
    notes = [f.note for f in check.run_check(matter, CONFIG).of("names")]
    assert any(n.startswith("lines 2, 4:") and "same role" in n for n in notes), notes
    assert any(n.startswith("line 5:") and "'#'" in n for n in notes), notes


def test_check_and_where_console_shows_no_values(matter, tmp_path):
    _setup(matter)
    _leak(matter, f"\n{CLIENT} called.\n")
    proc = cli("check", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 5, console
    assert_no_secrets(console)
    assert "Known values still in the output:" in proc.stdout and "Report:" in proc.stdout
    proc = cli("where", str(matter), "CLIENT", "[MANAGER]", "PERSON-99", cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "CLIENT: names.txt line 2 (active, a role)" in proc.stdout
    assert "MANAGER: names.txt line 3 (active, a role)" in proc.stdout
    assert "PERSON-99: not in names.txt or the key" in proc.stdout
