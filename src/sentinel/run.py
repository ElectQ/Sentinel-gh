"""Daily pulse entrypoint: collect followee events, cross with trending, emit pulse JSON."""

import datetime as dt

from . import state as state_mod
from .analyzers import pulse as pulse_mod
from .collectors import followees, trending
from .gh import GitHub


def main() -> None:
    gh = GitHub()
    st = state_mod.load()

    followee_logins, new_events = followees.collect(gh, st)
    followees.archive(new_events)
    state_mod.save(st)
    print(f"followees: {len(followee_logins)}, new events: {len(new_events)}")

    trend = trending.fetch(gh)
    print(f"trending: available={trend['available']} date={trend['date']} repos={len(trend['repos'])}")

    # Pulse covers everything collected today, so same-day re-runs aggregate.
    today = dt.datetime.now(dt.UTC).date().isoformat()
    day_events = followees.collected_on(today)
    pulse = pulse_mod.build(day_events, trend, len(followee_logins))
    pulse_mod.write(pulse)
    print(
        f"pulse {pulse['date']}: circle_hot={len(pulse['circle_hot'])} "
        f"overlap={len(pulse['trending_overlap'])} releases={len(pulse['releases'])} "
        f"new_repos={len(pulse['new_repos'])}"
    )


if __name__ == "__main__":
    main()
