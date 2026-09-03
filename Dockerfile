# Test and tooling image for the ORFE upcoming-events pipeline.
#
#   docker-compose run --rm tests            # full pytest suite
#   docker-compose run --rm newsletter-example  # end-to-end variant generation
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=America/New_York

WORKDIR /app

# Dependencies first so the layer caches across source edits. requirements.txt
# carries the tzdata wheel as a zoneinfo fallback: this image currently ships the
# system tz database, but that is not guaranteed across base-image revisions, and
# without a tz database every ZoneInfo("America/New_York") in src/newsletter.py
# raises. tests/test_newsletter_variant.py asserts the lookup works.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["pytest", "-q"]
