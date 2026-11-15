"""v1.1: the add-on never makes a menu by itself. The schedule only completes, tells,
fills the choice list and reminds; the old buttons are ignored; Menu bijwerken keeps
the chosen recipes."""

from datetime import datetime, timedelta

from app import main, planner
from app.hass import HELPER_AAN
from app.ledger import WeekLog, new_folder_available
from app.main import PERIOD_WARNING, handle_command, run_round, tick
from app.planner import Plan
from app.web import make_app
from tests.fakes import CURRENT, NEXT, W, dish, make_plan
from tests.test_choose_web import BONUS, login, make

DO = W + timedelta(days=1)
SUNDAY = datetime(2026, 11, 1, 8, 0)  # the Sunday before the menu Wednesday W


def with_hits(ah):
    """Every search term of the folder finds the same three recipes."""
    ah.recipe_hits = {t: [101, 102, 103] for t in planner.search_terms(BONUS)}


async def test_sunday_schedule_never_builds_a_menu(tmp_path):
    ctx, ah = make(tmp_path)
    login(ah)
    with_hits(ah)
    now = SUNDAY
    while now <= SUNDAY.replace(hour=23):
        await tick(ctx, now)
        now += timedelta(minutes=30)
    log = ctx.ledger.load(W)
    assert log.plan is None and log.draft is None
    assert ah.writes == []
    # the folder is out: its choice list was filled in the background
    assert {e.id for e in ctx.catalog.entries(W)} == {101, 102, 103}
    # the only push of the day is the reminder to choose
    assert [n["message"] for n in ctx.hass.notifications()] == [main.REMINDER_CHOOSE]


async def test_old_buttons_are_ignored(aiohttp_client, tmp_path):
    ctx, ah = make(tmp_path)
    login(ah)
    for cmd in ("nu_maken", "nieuw_menu"):
        assert await handle_command(ctx, '{"command": "%s"}' % cmd) is None
    assert ah.writes == [] and ah.calls == []
    assert ctx.ledger.load(W).plan is None and ctx.hass.notifications() == []
    client = await aiohttp_client(make_app(ctx))
    for mode in ("nieuw_menu", "nu_maken", "schema"):
        r = await client.post("api/run", json={"mode": mode})
        assert r.status == 400, mode
    assert ah.writes == [] and ctx.ledger.load(W).plan is None


async def test_bijwerken_keeps_the_chosen_recipes(tmp_path):
    ctx, ah = make(tmp_path)
    login(ah)
    # confirmed with the old folder: its products 1 and 2 are on the list
    old = make_plan([dish(W, 101, [1]), dish(DO, 102, [2])])
    ah.listed = {1: 1, 2: 1}
    ctx.ledger.save(WeekLog(W, plan=old.to_dict(), added={1: 1, 2: 1}, target="list",
                            added_to="list", round_done=True, notified=True,
                            folder_period_start=CURRENT.start, period_warning=PERIOD_WARNING,
                            new_folder=NEXT.start))
    out = await run_round(ctx, SUNDAY, "bijwerken")
    assert out.status == "done"
    log = ctx.ledger.load(W)
    plan = Plan.from_dict(log.plan)
    assert {d.date: d.recipe.id for d in plan.dishes} == {W: 101, DO: 102}
    assert {d.date: d.persons for d in plan.dishes} == {W: 2, DO: 2}
    # the products of the new folder, and only the difference written
    assert plan.basket == {11: 1, 12: 1}
    assert ah.held() == {11: 1, 12: 1} and log.added == {11: 1, 12: 1}
    assert log.folder_period_start == NEXT.start and log.period_warning is None
    assert not new_folder_available(log)
    assert ah.calls.count("search_recipes") == 0  # no menu was chosen, only matched
    # a second tap has no new folder to work with
    n = len(ah.writes)
    assert (await run_round(ctx, SUNDAY, "bijwerken")).status == "skipped"
    assert len(ah.writes) == n


async def test_sunday_reminder_asks_to_choose(tmp_path):
    at_five = SUNDAY.replace(hour=17)
    for sub in ("leeg", "concept", "uit"):
        (tmp_path / sub).mkdir()
    ctx, ah = make(tmp_path / "leeg")
    await main.remind_order(ctx, at_five)
    await main.remind_order(ctx, at_five + timedelta(hours=1))  # once a day
    (note,) = ctx.hass.notifications()
    assert "Kies het menu" in note["message"]

    ctx, ah = make(tmp_path / "concept")
    draft = make_plan([dish(W, 101, [11])])
    ctx.ledger.save(WeekLog(W, draft=draft.to_dict()))
    await main.remind_order(ctx, at_five)
    (note,) = ctx.hass.notifications()
    assert "nog niet bevestigd" in note["message"]
    assert ah.calls == []  # no AH read for a week without a menu

    ctx, ah = make(tmp_path / "uit")
    ctx.hass.states[HELPER_AAN] = "off"  # Weekmenu staat uit: no reminder
    await main.remind_order(ctx, at_five)
    assert ctx.hass.notifications() == []
