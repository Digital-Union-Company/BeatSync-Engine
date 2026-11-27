"""Settings Preference Consistency R1: the saved preference, the in-memory setting and the value
on screen must all agree.

Two defects, both measured on `main` at `2097822a` before the fix:

* **F1 — a stale collision-policy radio.** `pref_collision` is built with
  `value=resolve_policy(_option(COLLISION_POLICY))`, evaluated once when `create_ui()` builds the
  tree. `create_ui()` runs once per process, so with `suffix` saved and in force a *fresh browser
  session* was served the construction-time `skip`: the radio said files would be skipped while
  both sorters were suffixing. R17-A had already found and fixed exactly this for the
  notification checkbox and left the radio two components away carrying it.

* **F2 — a failed Reset defaults misrepresented what the app was still configured to do.** The
  nine folder boxes came back empty and the radio came back at its default over a
  `preferences.json` that still held the operator's real values. R17-A R2 recorded that as a known
  "pre-existing R8 reporting inaccuracy… deliberately left for a separate decision". It was worse
  than inaccurate: the misreported radio value is a *programmatic component write*, so Gradio's
  `.change` handed it back to `_on_collision_policy_change`, which had no no-op guard and
  **persisted the default policy** -- a reset reported as failed silently performed part of
  itself and announced a save nobody asked for.

The fix is three small pieces, all reusing what already existed: `_settings_on_load` (the one
server-side `app.load`, widened from notifications to settings-wide), a
"a write that changes nothing writes nothing" guard on `_on_collision_policy_change` (the guard
`_on_notifications_enabled_change` already carried), and a Reset failure path that repaints from
the same readers the panel is constructed from. **No new preferences subsystem, no new option key,
no new schema and no second `app.load`.**

The tier here is the repository's established one for `gui.py`: the shipped function bodies are
AST-extracted and *executed* against a synthesised namespace with the real `user_prefs`,
`collision` and `desktop_notify` modules and a real temporary preferences file. Nothing about the
store or the policy is stubbed, so these are assertions about shipped behaviour rather than about a
re-implementation. What this tier cannot see -- that a *browser* ends up displaying the corrected
value -- is measured separately against real `gradio==6.19.0` by
`repro_f1_real_gradio.py`, recorded in the task evidence, and the counts are in the task report.
"""

from __future__ import annotations

import ast
import json
import os
import types

import pytest

from beatsync_fork import collision as fork_collision
from beatsync_fork import desktop_notify as fork_notify
from beatsync_fork import user_prefs as prefs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


def _tree() -> ast.Module:
    with open(_GUI, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_GUI)


def _find(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    node = _find(tree, name)
    assert node is not None, f"{name} is not defined in gui.py"
    return node


#: The shipped bodies under test. `_pref` and `_option` are extracted rather than stubbed so the
#: readers the Settings boxes are CONSTRUCTED from are the same ones the assertions run against.
#:
#: `_settings_on_load` is extracted only IF PRESENT, deliberately. These tests are run against
#: pristine `main` to prove they are not vacuous, and a namespace that refused to build there
#: would make every F2 test fail for a harness reason instead of for the defect it is about -- a
#: failure that looks like evidence and is not. Absence is asserted by the tests that are
#: actually about the page-load reader.
_WANTED = ("_pref", "_option", "_collision_policy", "_notifications_enabled",
           "_notify_settings_on_load",
           "_on_collision_policy_change", "_on_notifications_enabled_change",
           "_on_reset_folder_prefs")
_OPTIONAL = ("_settings_on_load",)

#: Nine real destinations, one per Settings folder box, in `DESTINATION_KEYS` order -- which is
#: also the order `pref_reset_btn.click` lists its outputs in. A test using blanks here could not
#: tell a preserved value from a cleared one.
_REAL_FOLDERS = {prefs.CLIPS_LIBRARY: r"J:\Media Library",
                 prefs.CLIPS_REJECTED: r"J:\Discarded",
                 prefs.RAW_CUTTER_OUTPUT: r"J:\Raw_Cutts",
                 prefs.AUDIO_LIBRARY: r"J:\Voice Library",
                 prefs.AUDIO_DISCARDED: r"J:\Voice Discarded",
                 prefs.AUDIO_CUTTER_OUTPUT: r"J:\Voice Cuts",
                 prefs.AUDIO_PREVIEW_LIBRARY: r"J:\Voice Keep",
                 prefs.AUDIO_PREVIEW_DISCARDED: r"J:\Voice Drop",
                 prefs.RENDER_OUTPUT: r"J:\Finished Videos"}


class _Prefs:
    """The real `user_prefs`, with `save_preferences` failures injected deliberately.

    A failed save is the whole subject of F2 and it cannot be provoked reliably by filesystem
    tricks alone on every platform, so the failure is injected at the one call the handler makes.
    Everything else -- the schema, the option defaulting, the atomic replace -- is the real module.
    """

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def __getattr__(self, name):
        return getattr(prefs, name)

    def save_preferences(self, path, preferences, options=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return "Could not save folder defaults: The process cannot access the file"
        return prefs.save_preferences(path, preferences, options)


class _Namespace:
    """A synthesised `gui` module namespace running the shipped Settings bodies."""

    def __init__(self, tmp_path, *, policy=fork_collision.SUFFIX, notifications="on",
                 folders=None, fail_times=0):
        self.path = os.path.join(str(tmp_path), "preferences.json")
        stored = prefs.empty_preferences()
        stored.update(_REAL_FOLDERS if folders is None else folders)
        assert not prefs.save_preferences(
            self.path, stored,
            {prefs.COLLISION_POLICY: policy,
             prefs.DESKTOP_NOTIFICATIONS: notifications}), "fixture write failed"

        self.folders = prefs.load_preferences(self.path)[0]
        self.options = prefs.load_options(self.path)
        self.prefs = _Prefs(fail_times)
        self.toasts: list = []

        namespace = {
            "fork_prefs": self.prefs,
            "fork_collision": fork_collision,
            "fork_notify": fork_notify,
            "PREFS_PATH": self.path,
            "_FOLDER_PREFS": self.folders,
            "_FOLDER_OPTIONS": self.options,
            "_folder_prefs_status": lambda text: text,
            "STATUS_COLLISION_SAVED_SKIP": "saved: skip",
            "STATUS_COLLISION_SAVED_SUFFIX": "saved: suffix",
            "STATUS_NOTIFY_SAVED_ON": "saved: notifications on",
            "STATUS_NOTIFY_SAVED_OFF": "saved: notifications off",
            "gr": types.SimpleNamespace(
                Info=lambda text: self.toasts.append(("info", text)),
                Warning=lambda text: self.toasts.append(("warning", text)),
                update=lambda **kwargs: ("gr.update", kwargs)),
        }
        tree = _tree()
        body = [_func(tree, name) for name in _WANTED]
        body += [node for node in (_find(tree, name) for name in _OPTIONAL) if node is not None]
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                     _GUI, "exec"), namespace)                       # noqa: S102 - under test
        self.ns = namespace

    def __getitem__(self, name):
        return self.ns[name]

    # --- what is actually on disk, read back through the real store ---------------------------
    def saved_policy(self) -> str:
        return prefs.load_options(self.path)[prefs.COLLISION_POLICY]

    def saved_notifications(self) -> str:
        return prefs.load_options(self.path)[prefs.DESKTOP_NOTIFICATIONS]

    def saved_folders(self) -> dict:
        return prefs.load_preferences(self.path)[0]

    def raw(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()

    def loaded_radio(self):
        """The value `_settings_on_load` would write into the radio, unwrapped.

        The explicit absence check is the F1 defect stated as a message: on a tree with no
        settings-wide page-load reader there is NOTHING that corrects the radio for a new browser
        session, which is exactly the finding.
        """
        assert "_settings_on_load" in self.ns, (
            "no server-side page-load reader exists, so a fresh browser session is served the "
            "construction-time radio value for the life of the process (F1)")
        _toggle, _sentence, radio = self["_settings_on_load"]()
        assert radio[0] == "gr.update", radio
        return radio[1]["value"]


# =============================================================================================
# F1 -- a fresh browser session displays the policy that is actually in force
# =============================================================================================

def test_the_page_load_reader_answers_the_live_policy_not_a_construction_time_one(tmp_path):
    """The defect in one assertion: the value a fresh session is corrected to must follow the
    stored setting, not the value that happened to be stored when the process started."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP)
    assert ns.loaded_radio() == fork_collision.SKIP

    ns["_on_collision_policy_change"](fork_collision.SUFFIX)

    assert ns.saved_policy() == fork_collision.SUFFIX
    assert ns["_collision_policy"]() == fork_collision.SUFFIX
    assert ns.loaded_radio() == fork_collision.SUFFIX, (
        "a fresh browser session would still be told the old policy")


def test_the_page_load_reader_writes_all_three_saved_settings_in_the_registered_order(tmp_path):
    """Gradio matches `outputs` by position, so the composed reader's arity and order are part of
    the contract -- a tuple that grew without its registration writes the sentence into the
    radio."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, notifications="on")
    toggle, sentence, radio = ns["_settings_on_load"]()
    assert toggle == ("gr.update", {"value": True})
    assert sentence == fork_notify.app_setting_sentence(True)
    assert radio == ("gr.update", {"value": fork_collision.SUFFIX})

    call = next(node for node in ast.walk(_tree())
                if isinstance(node, ast.Call)
                and ast.unparse(node.func) == "app.load"
                and not any(keyword.arg == "js" for keyword in node.keywords))
    outputs = next(keyword.value for keyword in call.keywords if keyword.arg == "outputs")
    assert [ast.unparse(element) for element in outputs.elts] == [
        "notify_enabled", "notify_app_state", "pref_collision"]
    assert len(ns["_settings_on_load"]()) == len(outputs.elts)


def test_the_page_load_reader_is_a_composition_and_not_a_second_interpretation(tmp_path):
    """One reader per setting. A load path that resolved the policy itself would be a second place
    for the answer to drift -- the mistake `_collision_policy`'s own docstring warns about."""
    body = ast.unparse(_func(_tree(), "_settings_on_load"))
    assert "_notify_settings_on_load()" in body
    assert "_collision_policy()" in body
    assert "resolve_policy" not in body, "the policy is resolved by its own reader, not here"
    assert "OPTION_DEFAULTS" not in body


def test_the_page_load_reader_touches_no_file_and_writes_no_preference(tmp_path):
    """It reads in-memory dictionaries. A page load is not a save."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX)
    before = ns.raw()
    for _ in range(5):
        ns.loaded_radio()
    assert ns.raw() == before
    assert ns.prefs.calls == 0, "a page load called save_preferences"
    assert ns.toasts == []

    body = ast.unparse(_func(_tree(), "_settings_on_load"))
    for forbidden in ("open(", "os.", "json", "save_preferences", "subprocess", "requests"):
        assert forbidden not in body, forbidden


@pytest.mark.parametrize("stored", ["", "nonsense", "SUFFIX", None])
def test_a_corrupt_stored_policy_loads_as_the_safe_default_and_is_not_rewritten(tmp_path, stored):
    """`resolve_policy` is total, so the panel is corrected to a value that is actually in its
    `choices` list -- and the programmatic echo of that default must not persist it over whatever
    the operator's file says."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX)
    ns.options[prefs.COLLISION_POLICY] = stored
    loaded = ns.loaded_radio()
    assert loaded in (fork_collision.SKIP, fork_collision.SUFFIX)
    assert loaded == fork_collision.resolve_policy(stored)

    before = ns.raw()
    ns["_on_collision_policy_change"](loaded)
    assert ns.raw() == before, "echoing the resolved default rewrote the file"
    assert ns.toasts == []


# =============================================================================================
# F1 -- the programmatic write the correction performs must be inert
# =============================================================================================

def test_a_page_load_echo_writes_no_preference_and_raises_no_toast(tmp_path):
    """Gradio's `.change` fires for a function update as well as a gesture, so the correction
    re-enters the change handler. Without the guard every new browser session rewrote
    `preferences.json` and toasted a save the operator never asked for."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX)
    before = ns.raw()

    ns["_on_collision_policy_change"](ns.loaded_radio())

    assert ns.raw() == before
    assert ns.prefs.calls == 0
    assert ns.toasts == []
    assert ns.saved_policy() == fork_collision.SUFFIX


def test_a_stale_browser_session_cannot_silently_revert_the_saved_policy(tmp_path):
    """The measured route, on `2097822a`: tab A picks `suffix`; tab B loads, is served the
    construction-time `skip`, and the unguarded echo writes `skip` back -- reverting a setting
    nobody touched, with an "already taken will be skipped" toast as the only sign."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP)
    ns["_on_collision_policy_change"](fork_collision.SUFFIX)
    assert ns.saved_policy() == fork_collision.SUFFIX
    ns.toasts.clear()

    # The correction a fresh session now receives, echoed back as Gradio would.
    ns["_on_collision_policy_change"](ns.loaded_radio())

    assert ns.saved_policy() == fork_collision.SUFFIX, "a page load reverted the saved policy"
    assert ns["_collision_policy"]() == fork_collision.SUFFIX
    assert ns.toasts == []


def test_repeated_identical_updates_write_once_and_a_real_change_still_writes(tmp_path):
    """The guard must not become a refusal. Idempotence is the property; deafness is not."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP)

    for _ in range(4):
        ns["_on_collision_policy_change"](fork_collision.SKIP)
    assert ns.prefs.calls == 0, "an unchanged policy was written"
    assert ns.toasts == []

    ns["_on_collision_policy_change"](fork_collision.SUFFIX)
    assert ns.prefs.calls == 1
    assert ns.saved_policy() == fork_collision.SUFFIX
    assert ns.toasts == [("info", "saved: suffix")]

    ns["_on_collision_policy_change"](fork_collision.SKIP)
    assert ns.prefs.calls == 2
    assert ns.saved_policy() == fork_collision.SKIP
    assert ns.toasts[-1] == ("info", "saved: skip")


def test_a_real_change_that_cannot_be_saved_warns_and_still_applies_to_the_session(tmp_path):
    """Unchanged behaviour, pinned because the guard sits immediately above it: only persistence
    failed, and the in-memory value still governs both sorters."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP, fail_times=1)

    ns["_on_collision_policy_change"](fork_collision.SUFFIX)

    assert ns["_collision_policy"]() == fork_collision.SUFFIX
    assert ns.saved_policy() == fork_collision.SKIP, "the file should be unchanged"
    assert [kind for kind, _ in ns.toasts] == ["warning"]
    assert "session only" in ns.toasts[0][1]


# =============================================================================================
# F2 -- a failed Reset defaults reports what is still in force
# =============================================================================================

def _reset_outputs() -> list[str]:
    call = next(node for node in ast.walk(_tree())
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) == "pref_reset_btn.click")
    listed = next(keyword.value for keyword in call.keywords if keyword.arg == "outputs")
    return [ast.unparse(element) for element in listed.elts]


def test_a_failed_reset_preserves_every_folder_default_it_could_not_clear(tmp_path):
    """F2. Nothing was cleared, so nothing may be shown as cleared -- and the operator must not be
    left retyping nine paths that are still perfectly well saved."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, fail_times=99)

    returned = ns["_on_reset_folder_prefs"]()

    assert returned[-1].startswith("❌")
    assert list(returned[:9]) == [_REAL_FOLDERS[key] for key in prefs.DESTINATION_KEYS]
    assert ns.saved_folders() == ns.folders, "the file changed despite the failure"
    for key in prefs.DESTINATION_KEYS:
        assert ns.saved_folders()[key] == _REAL_FOLDERS[key]


def test_a_failed_reset_preserves_the_collision_policy_it_could_not_clear(tmp_path):
    """The half with a durable cost: the misreported default used to be echoed back and
    persisted."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, fail_times=99)

    returned = ns["_on_reset_folder_prefs"]()

    assert returned[9] == fork_collision.SUFFIX
    assert ns["_collision_policy"]() == fork_collision.SUFFIX
    assert ns.saved_policy() == fork_collision.SUFFIX


def test_a_failed_reset_still_reports_the_notification_setting_in_force(tmp_path):
    """R17-A R2's half of this path, re-pinned so widening the tuple cannot regress it."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, notifications="on", fail_times=99)

    returned = ns["_on_reset_folder_prefs"]()

    assert returned[10] == ("gr.update", {"value": True})
    assert returned[11] == fork_notify.app_setting_sentence(True)
    assert ns["_notifications_enabled"]() is True
    assert ns.saved_notifications() == "on"


def test_a_failed_reset_echoed_back_by_gradio_persists_nothing(tmp_path):
    """The composition that was the real defect, asserted end to end: a reset reported as FAILED
    must not perform any part of itself when the radio value it returned re-enters `.change`."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, fail_times=1)
    before = ns.raw()

    returned = ns["_on_reset_folder_prefs"]()
    assert returned[-1].startswith("❌")
    ns.toasts.clear()
    ns["_on_collision_policy_change"](returned[9])

    assert ns.raw() == before, "a failed reset persisted something through the radio echo"
    assert ns.saved_policy() == fork_collision.SUFFIX
    assert ns.saved_notifications() == "on"
    assert ns.toasts == [], "a failed reset announced a save"


def test_a_successful_reset_clears_the_folders_and_returns_the_policy_to_the_default(tmp_path):
    """The reset must still work. Nothing here is weakened by the failure path above."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, notifications="on")

    returned = ns["_on_reset_folder_prefs"]()

    assert list(returned[:9]) == [""] * 9
    assert returned[9] == prefs.OPTION_DEFAULTS[prefs.COLLISION_POLICY]
    assert returned[10] == ("gr.update", {"value": False})
    assert returned[-1].startswith("✅")
    assert ns.saved_policy() == prefs.OPTION_DEFAULTS[prefs.COLLISION_POLICY]
    assert ns.saved_notifications() == fork_notify.ENABLED_OFF
    assert all(ns.saved_folders()[key] == "" for key in prefs.KEYS)


def test_a_successful_reset_echoed_back_produces_no_second_save_and_no_second_toast(tmp_path):
    """Pre-existing and also closed by the guard: Reset writes the radio, so its own value
    re-entered the change handler and produced a duplicate save and a duplicate toast."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX)

    returned = ns["_on_reset_folder_prefs"]()
    saves_after_reset = ns.prefs.calls
    ns.toasts.clear()
    snapshot = ns.raw()

    ns["_on_collision_policy_change"](returned[9])

    assert ns.prefs.calls == saves_after_reset, "the reset's own echo saved a second time"
    assert ns.raw() == snapshot
    assert ns.toasts == []


def test_every_reset_return_is_a_flat_tuple_of_one_value_per_output():
    """Gradio matches by position, so a star-unpack or a nested tuple would silently write the
    status line into a folder box. `tests/test_audio_preview_tab.py` asserts this too; it is
    repeated here because this change is what made the failure path stop being nine literals."""
    outputs = _reset_outputs()
    returns = [node for node in ast.walk(_func(_tree(), "_on_reset_folder_prefs"))
               if isinstance(node, ast.Return)]
    assert len(returns) == 2, "one success path and one failure path"
    for node in returns:
        assert isinstance(node.value, ast.Tuple)
        assert len(node.value.elts) == len(outputs)
        assert not any(isinstance(element, ast.Starred) for element in node.value.elts)


def test_the_nine_preserved_folders_are_read_in_the_registrations_own_order():
    """The failure path's nine `_pref` calls are positional. Asserting the correspondence rather
    than trusting it, because swapping two of them would hand the operator each other's paths."""
    outputs = _reset_outputs()[:9]
    # the failure return is the one carrying the ❌ status
    returns = [node for node in ast.walk(_func(_tree(), "_on_reset_folder_prefs"))
               if isinstance(node, ast.Return)]
    failure = next(node for node in returns if "❌" in ast.unparse(node))
    read = [ast.unparse(element) for element in failure.value.elts[:9]]
    expected = [f"_pref(fork_prefs.{key.upper()})" for key in
                ("CLIPS_LIBRARY", "CLIPS_REJECTED", "RAW_CUTTER_OUTPUT",
                 "AUDIO_LIBRARY", "AUDIO_DISCARDED", "AUDIO_CUTTER_OUTPUT",
                 "AUDIO_PREVIEW_LIBRARY", "AUDIO_PREVIEW_DISCARDED", "RENDER_OUTPUT")]
    assert read == expected
    # and the widget order the values are written into matches the key order they are read from
    assert outputs == ["pref_library", "pref_rejected", "pref_raw_output",
                       "pref_audio_library", "pref_audio_discarded", "pref_audio_output",
                       "pref_audio_preview_library", "pref_audio_preview_discarded",
                       "pref_render_output"]
    assert len(prefs.DESTINATION_KEYS) == 9


# =============================================================================================
# the two options must not overwrite one another
# =============================================================================================

def test_changing_the_collision_policy_preserves_the_notification_setting(tmp_path):
    """R17-A established this; re-pinned because the guard now sits in front of the write."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP, notifications="on")

    ns["_on_collision_policy_change"](fork_collision.SUFFIX)

    assert ns.saved_policy() == fork_collision.SUFFIX
    assert ns.saved_notifications() == "on"
    assert ns["_notifications_enabled"]() is True


def test_changing_the_notification_setting_preserves_the_collision_policy(tmp_path):
    """The same hazard in the other direction: `save_preferences` replaces the whole file and
    defaults any option key the caller omits."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, notifications="off")

    ns["_on_notifications_enabled_change"](True)

    assert ns.saved_notifications() == fork_notify.ENABLED_ON
    assert ns.saved_policy() == fork_collision.SUFFIX, "the policy was reset to its default"
    assert ns["_collision_policy"]() == fork_collision.SUFFIX


def test_a_page_load_echoing_both_settings_changes_neither(tmp_path):
    """The whole point of the feature: opening a tab is not a settings change."""
    ns = _Namespace(tmp_path, policy=fork_collision.SUFFIX, notifications="on")
    before = ns.raw()

    toggle, _sentence, radio = ns["_settings_on_load"]()
    ns["_on_notifications_enabled_change"](toggle[1]["value"])
    ns["_on_collision_policy_change"](radio[1]["value"])

    assert ns.raw() == before
    assert ns.prefs.calls == 0
    assert ns.toasts == []


# =============================================================================================
# no new subsystem
# =============================================================================================

def test_no_new_preference_key_schema_or_store_was_introduced():
    """"Do not introduce a new preferences subsystem" -- asserted, not promised."""
    assert prefs.OPTION_KEYS == (prefs.COLLISION_POLICY, prefs.DESKTOP_NOTIFICATIONS)
    assert prefs.SCHEMA == "beatsync-folder-preferences-1"
    assert prefs.OPTION_DEFAULTS == {prefs.COLLISION_POLICY: "skip",
                                     prefs.DESKTOP_NOTIFICATIONS: "off"}
    assert len(prefs.DESTINATION_KEYS) == 9


def test_the_authorised_app_load_count_is_unchanged_and_one_is_server_side():
    """F1 was fixed by widening the existing registration. A sixth `app.load`, or a second
    server-side one, is a different design than the one reviewed."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("app.load(") == 5
    loads = [node for node in ast.walk(_tree())
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "app.load"]
    server_side = [node for node in loads
                   if not any(keyword.arg == "js" for keyword in node.keywords)]
    assert len(server_side) == 1


def test_the_radio_still_carries_a_construction_time_value_for_the_first_paint():
    """The `value=` is deliberately KEPT. Dropping it would leave the radio blank until the load
    event arrived, which is a worse first paint than a value that is right for the first session
    and corrected for every later one."""
    tree = _tree()
    radio = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == "gr.Radio"
                 and any(keyword.arg == "elem_id"
                         and getattr(keyword.value, "value", None) == "pref-collision-policy"
                         for keyword in node.keywords))
    value = next(keyword.value for keyword in radio.keywords if keyword.arg == "value")
    assert "resolve_policy" in ast.unparse(value)
    assert "COLLISION_POLICY" in ast.unparse(value)


def test_the_change_handler_guard_compares_resolved_policies(tmp_path):
    """Comparing the raw widget value would let a differently-spelled but equivalent value through,
    and comparing against the defaults table would reintroduce the second interpretation."""
    body = ast.unparse(_func(_tree(), "_on_collision_policy_change"))
    assert "resolved = fork_collision.resolve_policy(policy)" in body
    assert "if resolved == _collision_policy():" in body
    # and the guard precedes the mutation of the in-memory option
    guard = body.index("if resolved == _collision_policy():")
    write = body.index("_FOLDER_OPTIONS[fork_prefs.COLLISION_POLICY] = resolved")
    assert guard < write, "the guard must sit before the write it protects"


def test_the_saved_file_keeps_both_options_and_the_documented_shape(tmp_path):
    """One file, two option keys, the same schema -- read back as JSON rather than through the
    module, so a change to the persisted shape cannot hide behind its own loader."""
    ns = _Namespace(tmp_path, policy=fork_collision.SKIP, notifications="off")
    ns["_on_collision_policy_change"](fork_collision.SUFFIX)

    with open(ns.path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["schema"] == prefs.SCHEMA
    assert set(payload) == {"schema", "folders", "options"}
    assert payload["options"] == {prefs.COLLISION_POLICY: fork_collision.SUFFIX,
                                  prefs.DESKTOP_NOTIFICATIONS: "off"}
    assert payload["folders"][prefs.CLIPS_LIBRARY] == _REAL_FOLDERS[prefs.CLIPS_LIBRARY]
