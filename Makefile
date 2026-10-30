PIP ?= python3 -m pip

.PHONY: build-RainCheckFunction

build-RainCheckFunction:
	mkdir -p "$(ARTIFACTS_DIR)"
	# Explicit copies keep local databases, credentials, and fixtures out of the zip.
	cp src/db.py src/digest.py src/ebay_pricing.py src/http_retry.py src/lambda_handler.py src/listing_images.py src/matching.py src/schema.sql src/sendemail.py src/yahoo_scrape.py "$(ARTIFACTS_DIR)/"
	$(PIP) install --disable-pip-version-check --requirement requirements.txt --target "$(ARTIFACTS_DIR)"
	# Some pinned wheels include their own tests; prune only inside the finished artifact.
	find "$(ARTIFACTS_DIR)" -type d \( -name tests -o -name test -o -name __pycache__ \) -prune -exec rm -rf -- {} +
	find "$(ARTIFACTS_DIR)" -type f \( -name 'test_*' -o -name '*_test.py' -o -name '*.pyc' -o -name '*.pyo' \) -delete
