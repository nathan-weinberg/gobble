import datetime
import pandas as pd
import pathlib
import shutil
import urllib.request
import warnings

MAIN_DIR = pathlib.Path("./data/gtfs_archives/")
MAIN_DIR.mkdir(parents=True, exist_ok=True)

GTFS_ARCHIVES_PREFIX = pathlib.Path("https://cdn.mbta.com/archive/")
GTFS_ARCHIVES_FILENAME = "archived_feeds.txt"


def _download_gtfs_archives_list():
    """Downloads list of GTFS archive urls. This file will get overwritten."""
    archives_df = pd.read_csv(GTFS_ARCHIVES_PREFIX / GTFS_ARCHIVES_FILENAME)
    archives_df.to_csv(MAIN_DIR / GTFS_ARCHIVES_FILENAME)
    return archives_df


def to_dateint(date):
    """turn date into 20220615 e.g."""
    return int(str(date).replace("-", ""))


def get_gtfs_archive(dateint: int):
    """
    Determine which GTFS archive corresponds to the date.
    Returns that archive folder, downloading if it doesn't yet exist.
    """
    matches = pd.DataFrame()
    if (MAIN_DIR / GTFS_ARCHIVES_FILENAME).exists():
        archives_df = pd.read_csv(MAIN_DIR / GTFS_ARCHIVES_FILENAME)
        matches = archives_df[(archives_df.feed_start_date <= dateint) & (archives_df.feed_end_date >= dateint)]

    # if there are no matches or we havent downloaded the url list yet,
    # fetch (or refetch) the archives and seek matches
    if len(matches) == 0:
        print("No matches found in existing GTFS archives. Fetching latest archives.")
        archives_df = _download_gtfs_archives_list()
        matches = archives_df[(archives_df.feed_start_date <= dateint) & (archives_df.feed_end_date >= dateint)]

    archive_url = matches.iloc[0].archive_url

    archive_name = pathlib.Path(archive_url).stem

    if (MAIN_DIR / archive_name).exists():
        print(f"GTFS archive for {dateint} already downloaded: {archive_name}")
        return MAIN_DIR / archive_name

    # else we have to download it
    print(f"Downloading GTFS archive for {dateint}: {archive_url}")
    zipfile, _ = urllib.request.urlretrieve(archive_url)
    shutil.unpack_archive(zipfile, extract_dir=(MAIN_DIR / archive_name), format="zip")
    # remove temporary zipfile
    urllib.request.urlcleanup()

    return MAIN_DIR / archive_name


def get_services(date: datetime.date, archive_dir: pathlib.Path):
    """
    Read calendar.txt to determine which services ran on the given date.
    Also, incorporate exceptions from calendar_dates.txt for holidays, etc.
    """
    dateint = to_dateint(date)
    day_of_week = date.strftime("%A").lower()

    cal = pd.read_csv(archive_dir / "calendar.txt")
    current_services = cal[(cal.start_date <= dateint) & (cal.end_date >= dateint)]
    services = current_services[current_services[day_of_week] == 1].service_id.tolist()

    exceptions = pd.read_csv(archive_dir / "calendar_dates.txt")
    exceptions = exceptions[exceptions.date == dateint]
    additions = exceptions[exceptions.exception_type == 1].service_id.tolist()
    subtractions = exceptions[exceptions.exception_type == 2].service_id.tolist()

    services = (set(services) - set(subtractions)) | set(additions)
    return list(services)


def read_gtfs(date: datetime.date):
    """
    Given a date, this function will:
    - Find the appropriate gtfs archive (downloading if necessary)
    - Determine which services ran on that date
    - Return two dataframes containing just the trips and stop_times that ran on that date
    """
    dateint = to_dateint(date)

    archive_dir = get_gtfs_archive(dateint)
    services = get_services(date, archive_dir)

    # specify dtypes to avoid warnings
    trips = pd.read_csv(archive_dir / "trips.txt", dtype={"trip_short_name": str, "block_id": str})
    trips = trips[trips.service_id.isin(services)]

    stops = pd.read_csv(archive_dir / "stops.txt")

    stop_times = pd.read_csv(
        archive_dir / "stop_times.txt",
        dtype={"trip_id": str, "stop_id": str, "stop_headsign": str, "checkpoint_id": str},
    )
    stop_times = stop_times[stop_times.trip_id.isin(trips.trip_id)]
    stop_times.arrival_time = pd.to_timedelta(stop_times.arrival_time)
    stop_times.departure_time = pd.to_timedelta(stop_times.departure_time)

    return trips, stop_times, stops


def add_gtfs_headways(events_df: pd.DataFrame, all_trips: pd.DataFrame, all_stops: pd.DataFrame):
    """
    This will calculate scheduled headway and traveltime information
    from gtfs for the routes we care about, and then match our actual
    events to the scheduled values. This matching is done based on
    time-of-day, so is not an excact match. Luckily, pandas helps us out
    with merge_asof.
    https://pandas.pydata.org/docs/reference/api/pandas.merge_asof.html
    """
    # TODO: I think we need to worry about 114/116/117 headways?

    # defining these columns in particular becasue we use them everywhere
    RTE_DIR_STOP = ["route_id", "direction_id", "stop_id"]

    results = []
    # NB: event times are converted to pd timestamps in this fuction for pandas merge manipulation,
    # but will be converted back into datetime.datetime for serialization purposes. careful!
    events_df.event_time = events_df["event_time"].dt.tz_localize(None)

    # we have to do this day-by-day because gtfs changes so often
    for service_date, days_events in events_df.groupby("service_date"):
        # filter out the trips of interest
        relevant_trips = all_trips[all_trips.route_id.isin(days_events.route_id)]

        # take only the stops from those trips (adding route and dir info)
        trip_info = relevant_trips[["trip_id", "route_id", "direction_id"]]
        gtfs_stops = all_stops.merge(trip_info, on="trip_id", how="right")

        # calculate gtfs headways
        gtfs_stops = gtfs_stops.sort_values(by="arrival_time")
        headways = gtfs_stops.groupby(RTE_DIR_STOP).arrival_time.diff()
        # the first stop of a trip doesnt technically have a real scheduled headway, so we set to empty string
        headways = headways.fillna("")
        gtfs_stops["scheduled_headway"] = headways.dt.seconds

        # calculate gtfs traveltimes
        trip_start_times = gtfs_stops.groupby("trip_id").arrival_time.transform("min")
        gtfs_stops["scheduled_tt"] = (gtfs_stops.arrival_time - trip_start_times).dt.seconds

        # assign each actual timepoint a scheduled headway
        # merge_asof 'backward' matches the previous scheduled value of 'arrival_time'
        days_events["arrival_time"] = days_events.event_time - pd.Timestamp(service_date).tz_localize(None)
        augmented_events = pd.merge_asof(
            days_events.sort_values(by="arrival_time"),
            gtfs_stops[RTE_DIR_STOP + ["arrival_time", "scheduled_headway"]],
            on="arrival_time",
            direction="backward",
            by=RTE_DIR_STOP,
        )

        # assign each actual trip a scheduled trip_id, based on when it started the route
        route_starts = days_events.loc[days_events.groupby("trip_id").event_time.idxmin()]
        route_starts = route_starts[RTE_DIR_STOP + ["trip_id", "arrival_time"]]

        trip_id_map = pd.merge_asof(
            route_starts.sort_values(by="arrival_time"),
            gtfs_stops[RTE_DIR_STOP + ["arrival_time", "trip_id"]],
            on="arrival_time",
            direction="nearest",
            by=RTE_DIR_STOP,
            suffixes=["", "_scheduled"],
        )
        trip_id_map = trip_id_map.set_index("trip_id").trip_id_scheduled

        # use the scheduled trip matching to get the scheduled traveltime
        augmented_events["scheduled_trip_id"] = augmented_events.trip_id.map(trip_id_map)
        augmented_events = pd.merge(
            augmented_events,
            gtfs_stops[RTE_DIR_STOP + ["trip_id", "scheduled_tt"]],
            how="left",
            left_on=RTE_DIR_STOP + ["scheduled_trip_id"],
            right_on=RTE_DIR_STOP + ["trip_id"],
            suffixes=["", "_gtfs"],
        )

        # finally, put all the days together
        results.append(augmented_events)

    results_df = pd.concat(results)
    # convert to event_time python datetime for serialization purposes outside of this fuction
    # TODO: do we also do this with arrival_time? its not written to disk
    # future warning: returning a series is actually the correct future behavior of to_pydatetime(), can drop the
    # context manager later
    with warnings.catch_warnings():
        warnings.simplefilter(action="ignore", category=FutureWarning)
        results_df["event_time"] = pd.Series(results_df["event_time"].dt.to_pydatetime(), dtype="object")
    return results_df
