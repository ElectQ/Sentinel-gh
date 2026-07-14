"""Daily pipeline: followee events + following diffs → feed + pulse + bundles.

Bundles (Soundwave-compatible contract) are the downstream entry point:
  bundles/index.json + bundles/<beijing-date>.json
"""

import datetime as dt

from . import state as state_mod
from .analyzers import bundle as bundle_mod
from .analyzers import feed as feed_mod
from .analyzers import pulse as pulse_mod
from .collectors import followees, following, trending
from .gh import GitHub
from .util import env_bool


def main() -> None:
    gh = GitHub()
    st_users = state_mod.load_followees()
    st_following = state_mod.load_following()

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

    state_mod.save_followees(st_users)
    state_mod.save_following(st_following)

    trend = trending.fetch(gh)
    print(
        f"trending: available={trend['available']} date={trend['date']} "
        f"repos={len(trend['repos'])}"
    )

    # Internal collection key stays UTC (_collected_at[:10]).
    utc_today = dt.datetime.now(dt.UTC).date().isoformat()
    # Contract day follows Soundwave: Asia/Shanghai calendar date.
    bj_today = bundle_mod.beijing_date()
    day_events = followees.collected_on(utc_today)
    day_follows = following.collected_on(utc_today)
    trunc = following.truncated_actors(st_following)

    feed_item_count = None
    feed = None
    if env_bool("FEED_ENABLED", True):
        feed = feed_mod.build(
            day_events, day_follows, trend, len(followee_logins), date=bj_today, gh=gh
        )
        feed_mod.write(feed)
        feed_item_count = feed["item_count"]
        print(
            f"feed {feed['date']}: items={feed['item_count']} "
            f"by_kind={feed['summary']['by_kind']}"
        )

        if env_bool("BUNDLE_ENABLED", True):
            bpath = bundle_mod.write_bundle_from_feed(feed, collect_date=bj_today)
            print(f"bundle: {bpath} items={feed['item_count']}")

    pulse = pulse_mod.build(
        day_events,
        day_follows,
        trend,
        len(followee_logins),
        feed_item_count=feed_item_count,
        following_enabled=env_bool("FOLLOWING_ENABLED", True),
        truncated_users=trunc,
    )
    # Align pulse date with contract day
    pulse["date"] = bj_today
    pulse_mod.write(pulse)
    print(
        f"pulse {pulse['date']}: circle_hot={len(pulse['circle_hot'])} "
        f"overlap={len(pulse['trending_overlap'])} "
        f"stars={pulse['stars']['total']} forks={len(pulse['forks'])} "
        f"follows={pulse['follows']['new_count']} "
        f"releases={len(pulse['releases'])} new_repos={len(pulse['new_repos'])}"
    )

    if gh.last_rate_limit_remaining is not None:
        print(f"rate_limit: remaining={gh.last_rate_limit_remaining}")


if __name__ == "__main__":
    main()
