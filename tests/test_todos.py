import pytest

from broker import todos as T

FILE = """# jobs

## Pending
- [0] Ask about the retry policy
  Why: the spec is silent
- [1] Fix roster sync retry
  Notes: see the 2026-11-02 log

## Done
- [x] Collector and hook
- [3] Tracker v1
"""


def item(text, body):
    return next(i for i in T.parse(text) if i.text == body)


def test_reads_integer_and_legacy_states_with_their_sections():
    items = T.parse(FILE)
    assert [(i.state, i.text, i.section, i.legacy) for i in items] == [
        (0, "Ask about the retry policy", "Pending", False),
        (1, "Fix roster sync retry", "Pending", False),
        (3, "Collector and hook", "Done", True),
        (3, "Tracker v1", "Done", False),
    ]


def test_moving_an_item_changes_only_its_bracket():
    before = item(FILE, "Fix roster sync retry")
    after = T.set_state(FILE, before.id, expected=1, to=2)
    assert after.replace("- [2] Fix roster sync retry", "- [1] Fix roster sync retry") == FILE
    assert "  Notes: see the 2026-11-02 log" in after


def test_a_stale_view_is_refused_and_nothing_changes():
    target = item(FILE, "Ask about the retry policy")
    with pytest.raises(T.TodoError, match="unapproved, not approved"):
        T.set_state(FILE, target.id, expected=1, to=2)


@pytest.mark.parametrize("expected,to,body", [
    (0, 3, "Ask about the retry policy"),
    (1, 3, "Fix roster sync retry"),
    (3, 0, "Tracker v1"),
    (0, 2, "Ask about the retry policy"),
])
def test_moves_outside_the_allowed_set_are_refused(expected, to, body):
    target = item(FILE, body)
    assert target.state == expected  # the item really is in the state the caller claims
    with pytest.raises(T.TodoError, match="can't go from"):
        T.set_state(FILE, target.id, expected=expected, to=to)


def test_a_legacy_unchecked_box_is_approved_into_the_integer_form():
    text = "## Pending\n- [ ] Old item\n"
    target = item(text, "Old item")
    assert target.legacy and target.state == 0
    assert T.set_state(text, target.id, expected=0, to=1) == "## Pending\n- [1] Old item\n"


def test_identical_texts_get_distinct_ids_that_survive_a_change_to_the_other():
    text = "## Pending\n- [0] Same words\n- [1] Same words\n"
    first, second = T.parse(text)
    assert first.id != second.id
    moved = T.set_state(text, second.id, expected=1, to=2)
    assert [i.id for i in T.parse(moved)] == [first.id, second.id]
