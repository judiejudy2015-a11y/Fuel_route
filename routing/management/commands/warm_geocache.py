import time
import csv
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings

US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
    'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
    'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
    'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC'
}


class Command(BaseCommand):
    help = "Pre-warm the geocode cache for all city+state pairs"

    def handle(self, *args, **options):
        from routing.fuel_service import (
            _load_geocode_cache, _save_geocode_cache, _geocode_city_state
        )

        csv_path = Path(settings.FUEL_PRICES_CSV)
        city_states = set()

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            import csv as csv_module
            reader = csv_module.DictReader(f)
            for row in reader:
                state = row.get("State", "").strip()
                if state in US_STATES:
                    city = row.get("City", "").strip().rstrip()
                    city_states.add((city, state))

        self.stdout.write(f"Found {len(city_states)} unique city+state pairs")
        cache = _load_geocode_cache()
        missing = [(c, s) for (c, s) in city_states if f"{c}|{s}" not in cache]
        self.stdout.write(f"{len(missing)} need geocoding...")

        for i, (city, state) in enumerate(missing):
            _geocode_city_state(city, state, cache)
            if (i + 1) % 10 == 0:
                _save_geocode_cache(cache)
                self.stdout.write(f"  {i+1}/{len(missing)} done...")
            time.sleep(1.1)  # give OpenCage more breathing room

        _save_geocode_cache(cache)
        self.stdout.write(self.style.SUCCESS("Done! Cache is ready."))
