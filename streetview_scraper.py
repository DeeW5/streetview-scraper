import csv
import argparse
import math
import os
import random
import time
from pathlib import Path

import requests


API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")  # read the key without storing it here
CITY_CENTRES = {
    "london": (51.5074, -0.1278),
    "bangalore": (12.9716, 77.5946),
}
CENTRE = CITY_CENTRES["london"]
RADIUS_KM = 15  # sample points within this radius
NUMBER_OF_IMAGES = 2000  # number of images to save
OUTPUT_FOLDER = Path("data/london")  # images and manifest go here

IMAGE_SIZE = "640x640"  # largest supported static image size
HEADING = 0  # point the camera north
FIELD_OF_VIEW = 90  # keep the view reasonably wide
PITCH = 0  # keep the camera level
MINIMUM_DISTANCE_M = 50  # avoid collecting almost identical locations
METADATA_RADIUS_M = 100  # search this far from each random point
OFFICIAL_ONLY = False  # ignore non-Google copyright when enabled
RANDOM_SEED = None  # use None for a different sample each run

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"  # free lookup
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"  # billable image request
EARTH_RADIUS_M = 6_371_000  # average radius of the Earth


def random_point():
    radius_m = RADIUS_KM * 1000  # convert the configured radius to metres
    distance_from_centre = radius_m * math.sqrt(random.random())  # spread points evenly
    bearing = random.uniform(0, 2 * math.pi)  # choose a random compass direction
    angle = distance_from_centre / EARTH_RADIUS_M  # convert distance to an angle

    latitude = math.radians(CENTRE[0])  # trigonometry uses radians
    longitude = math.radians(CENTRE[1])
    new_latitude = math.asin(
        math.sin(latitude) * math.cos(angle)
        + math.cos(latitude) * math.sin(angle) * math.cos(bearing)
    )
    new_longitude = longitude + math.atan2(  # account for the Earth's curvature
        math.sin(bearing) * math.sin(angle) * math.cos(latitude),
        math.cos(angle) - math.sin(latitude) * math.sin(new_latitude),
    )
    return math.degrees(new_latitude), math.degrees(new_longitude)  # return normal coordinates


def distance_between(first, second):
    latitude_one, longitude_one = map(math.radians, first)  # first point in radians
    latitude_two, longitude_two = map(math.radians, second)  # second point in radians
    latitude_change = latitude_two - latitude_one
    longitude_change = longitude_two - longitude_one
    value = (  # haversine formula
        math.sin(latitude_change / 2) ** 2
        + math.cos(latitude_one)
        * math.cos(latitude_two)
        * math.sin(longitude_change / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(value))  # distance in metres


def get_new_point(previous_points):
    for _ in range(1000):  # stop trying if the area becomes too crowded
        point = random_point()  # make a candidate inside the circle
        if all(
            distance_between(point, old_point) >= MINIMUM_DISTANCE_M
            for old_point in previous_points
        ):
            return point  # this point is far enough from earlier samples
    raise RuntimeError("could not find another point; lower MINIMUM_DISTANCE_M")


def find_panorama(session, point):
    # Metadata lets us check coverage before paying for an image request.
    response = session.get(
        METADATA_URL,
        params={
            "location": f"{point[0]},{point[1]}",
            "radius": METADATA_RADIUS_M,
            "source": "outdoor",
            "key": API_KEY,
        },
        timeout=30,
    )
    response.raise_for_status()  # turn HTTP errors into useful exceptions
    metadata = response.json()  # Google returns the panorama details as JSON

    if metadata.get("status") != "OK":  # no panorama was found nearby
        return None
    if OFFICIAL_ONLY and "google" not in metadata.get("copyright", "").lower():
        return None  # skip imagery without Google's copyright
    return metadata  # keep the panorama ID for the image request


def download_image(session, metadata, filename):
    # Use the panorama ID returned by the metadata request.
    response = session.get(
        IMAGE_URL,
        params={
            "pano": metadata["pano_id"],
            "size": IMAGE_SIZE,
            "heading": HEADING,
            "fov": FIELD_OF_VIEW,
            "pitch": PITCH,
            "source": "outdoor",
            "key": API_KEY,
        },
        timeout=60,
    )
    response.raise_for_status()  # fail instead of saving an error page
    if not response.headers.get("content-type", "").startswith("image/"):
        raise RuntimeError("Street View did not return an image")
    filename.write_bytes(response.content)  # save the JPEG to disk


def write_manifest(rows):
    # Keep coordinates and panorama details beside the images.
    with (OUTPUT_FOLDER / "manifest.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["filename", "latitude", "longitude", "panorama_id", "date"],
        )
        writer.writeheader()  # add the CSV column names
        writer.writerows(rows)  # add one row per downloaded image


def load_existing_images():
    images = {}
    for filename in OUTPUT_FOLDER.glob("streetview_*.jpg"):
        try:
            image_number = int(filename.stem.rsplit("_", 1)[1])
        except ValueError:
            continue
        images[image_number] = filename
    return images


def load_manifest():
    manifest_path = OUTPUT_FOLDER / "manifest.csv"
    if not manifest_path.exists():
        return {}
    with manifest_path.open(newline="", encoding="utf-8") as file:
        return {row["filename"]: row for row in csv.DictReader(file)}


def append_manifest_row(row):
    manifest_path = OUTPUT_FOLDER / "manifest.csv"
    exists = manifest_path.exists()
    with manifest_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["filename", "latitude", "longitude", "panorama_id", "date"],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def request_with_retries(request_function, attempts=5):
    for attempt in range(attempts):
        try:
            return request_function()
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            print(f"request failed; retrying in {delay}s")
            time.sleep(delay)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Download random Google Street View images for a city."
    )
    parser.add_argument(
        "--city",
        choices=sorted(CITY_CENTRES),
        default="london",
        help="city to sample (default: london)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=NUMBER_OF_IMAGES,
        help=f"number of images to download (default: {NUMBER_OF_IMAGES})",
    )
    parser.add_argument(
        "--radius-km",
        type=float,
        default=RADIUS_KM,
        help=f"sampling radius in kilometres (default: {RADIUS_KM})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output folder (default: data/<city>)",
    )
    parser.add_argument(
        "--latitude",
        type=float,
        default=None,
        help="override the city-centre latitude",
    )
    parser.add_argument(
        "--longitude",
        type=float,
        default=None,
        help="override the city-centre longitude",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key; prefer GOOGLE_MAPS_API_KEY to avoid exposing it in process arguments",
    )
    parser.add_argument(
        "--official-only",
        action="store_true",
        help="keep only metadata whose copyright contains Google",
    )
    return parser.parse_args()


def main():
    global API_KEY, CENTRE, NUMBER_OF_IMAGES, OUTPUT_FOLDER, RADIUS_KM, OFFICIAL_ONLY

    arguments = parse_arguments()
    if (arguments.latitude is None) != (arguments.longitude is None):
        raise ValueError("--latitude and --longitude must be provided together")

    API_KEY = arguments.api_key or API_KEY
    CENTRE = (
        (arguments.latitude, arguments.longitude)
        if arguments.latitude is not None
        else CITY_CENTRES[arguments.city]
    )
    NUMBER_OF_IMAGES = arguments.count
    RADIUS_KM = arguments.radius_km
    OUTPUT_FOLDER = arguments.output or Path("data") / arguments.city
    OFFICIAL_ONLY = arguments.official_only

    if not API_KEY:
        raise ValueError("set GOOGLE_MAPS_API_KEY before running")
    if NUMBER_OF_IMAGES <= 0:
        raise ValueError("--count must be greater than zero")
    if RADIUS_KM <= 0:
        raise ValueError("--radius-km must be greater than zero")

    random.seed(RANDOM_SEED)  # make the sample repeatable
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)  # create the output folder
    existing_images = load_existing_images()
    existing_manifest = load_manifest()
    for image_number in sorted(existing_images):
        filename = existing_images[image_number].name
        if filename not in existing_manifest:
            append_manifest_row(
                {
                    "filename": filename,
                    "latitude": "",
                    "longitude": "",
                    "panorama_id": "",
                    "date": "",
                }
            )

    previous_points = []  # remember points already tested
    downloaded_count = len(existing_images)

    with requests.Session() as session:
        attempts = 0  # failed locations still count as attempts
        while downloaded_count < NUMBER_OF_IMAGES:
            attempts += 1  # avoid looping forever when coverage is sparse
            if attempts > NUMBER_OF_IMAGES * 20:
                raise RuntimeError(
                    f"only found {downloaded_count} usable images after {attempts} attempts"
                )

            point = get_new_point(previous_points)  # choose a fresh location
            previous_points.append(point)  # do not sample it again
            metadata = request_with_retries(
                lambda: find_panorama(session, point)
            )  # check for Street View
            if metadata is None:
                print(f"no Street View image near {point}")
                continue

            image_number = max(existing_images, default=0) + 1
            filename = OUTPUT_FOLDER / f"streetview_{image_number:05d}.jpg"
            request_with_retries(
                lambda: download_image(session, metadata, filename)
            )
            existing_images[image_number] = filename
            downloaded_count += 1
            append_manifest_row(
                {
                    "filename": filename.name,
                    "latitude": f"{point[0]:.7f}",
                    "longitude": f"{point[1]:.7f}",
                    "panorama_id": metadata["pano_id"],
                    "date": metadata.get("date", ""),
                }
            )
            print(f"{downloaded_count}/{NUMBER_OF_IMAGES}: {filename}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, requests.RequestException, KeyError) as error:
        raise SystemExit(f"error: {error}")
