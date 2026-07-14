"""Daily pipeline: followee events + following diffs → feed + pulse + bundles.

Bundles (Soundwave-compatible contract) are the downstream entry point:
  bundles/index.json + bundles/<beijing-date>.json
"""

from . import state as state_mod
from .analyzers import bundle as bundle_mod
from .analyzers import feed as feed_mod
from .analyzers import pulse as pulse_mod
from .collectors import followees, following, received, trending
from .gh import GitHub
from .util import beijing_day, env_bool


def _my_repos(gh: GitHub) -> set[str]:
    """Repos you own — excluded from outer-circle heat. Best-effort."""
    try:
        repos = gh.paginate("/user/repos", {"affiliation": "owner"})
        return {r["full_name"] for r in repos}
    except Exception:
        return set()


def main() -> None:
    gh = GitHub()
    st_users = state_mod.load_followees()
    st_following = state_mod.load_following()
    st_received = state_mod.load_received()

    followee_logins, new_events = followees.collect(gh, st_users)
    followees.archive(new_events)
    print(f"followees: {len(followee_logins)}, new events: {len(new_events)}")

    active_actors = {e["actor"] for e in new_events}
    if env_bool("FOLLOWING_ENABLED", True):
        new_diffs = following.collect(
            gh, st_following, followee_logins, active=active_actors
        )
        following.archive(new_diffs)
        print(f"following: this_run_diffs={len(new_diffs)}")
    else:
        new_diffs = []
        print("following: disabled")

    new_received: list[dict] = []
    if env_bool("RECEIVED_ENABLED", True):
        new_received = received.collect(gh, st_received)
        received.archive(new_received)
        print(f"received: login={st_received.get('login')} new={len(new_received)}")

    state_mod.save_followees(st_users)
    state_mod.save_following(st_following)
    state_mod.save_received(st_received)

    trend = trending.fetch(gh)
    print(
        f"trending: available={trend['available']} date={trend['date']} "
        f"repos={len(trend['repos'])}"
    )

    followee_set = set(followee_logins)
    my_repos = _my_repos(gh) if env_bool("RECEIVED_ENABLED", True) else set()
    trunc = following.truncated_actors(st_following)

    # Contract day = Beijing calendar date the event *happened* on. A run near
    # 06:00 CST therefore touches two days: it completes yesterday and opens
    # today. Rebuild every day the new records land in, from the full archive —
    # that keeps bundles idempotent under re-runs and backfills.
    days = {beijing_day(e["created_at"]) for e in new_events}
    days |= {beijing_day(f["observed_at"]) for f in new_diffs}
    days |= {beijing_day(e["created_at"]) for e in new_received}
    days.add(bundle_mod.beijing_date())

    for day in sorted(days):  # ascending, so latest.json ends on the newest day
        day_events = followees.events_on(day)
        day_follows = following.follows_on(day)
        day_received = received.received_on(day)

        feed_item_count = None
        if env_bool("FEED_ENABLED", True):
            feed = feed_mod.build(
                day_events, day_follows, trend, len(followee_logins), date=day, gh=gh,
                received_events=day_received, followees=followee_set, my_repos=my_repos,
            )
            feed_mod.write(feed)
            feed_item_count = feed["item_count"]
            print(
                f"feed {day}: items={feed['item_count']} "
                f"by_kind={feed['summary']['by_kind']}"
            )
            if env_bool("BUNDLE_ENABLED", True):
                bpath = bundle_mod.write_bundle_from_feed(
                    feed, collect_date=day, merge=False
                )
                print(f"bundle: {bpath} items={feed['item_count']}")

        pulse = pulse_mod.build(
            day_events,
            day_follows,
            trend,
            len(followee_logins),
            feed_item_count=feed_item_count,
            following_enabled=env_bool("FOLLOWING_ENABLED", True),
            truncated_users=trunc,
            received_events=day_received,
            followees=followee_set,
            my_repos=my_repos,
        )
        pulse["date"] = day
        pulse_mod.write(pulse)
        print(
            f"pulse {day}: circle_hot={len(pulse['circle_hot'])} "
            f"outer_hot={len(pulse['circle_outer_hot'])} "
            f"overlap={len(pulse['trending_overlap'])} "
            f"stars={pulse['stars']['total']} forks={len(pulse['forks'])} "
            f"follows={pulse['follows']['new_count']} "
            f"releases={len(pulse['releases'])} new_repos={len(pulse['new_repos'])}"
        )

    if gh.last_rate_limit_remaining is not None:
        print(f"rate_limit: remaining={gh.last_rate_limit_remaining}")


if __name__ == "__main__":
    main()
