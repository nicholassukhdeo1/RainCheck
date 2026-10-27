PIP ?= python3 -m pip

.PHONY: build-RainCheckFunction

build-RainCheckFunction:
	mkdir -p "$(ARTIFACTS_DIR)"
	# Explicit copies keep local databases, credentials, and fixtures out of the zip.
	cp src/db.py src/digest.py src/ebay_pricing.py src/http_retry.py src/lambda_handler.py src/matching.py src/schema.sql src/sendemail.py src/yahoo_scrape.py "$(ARTIFACTS_DIR)/"
	$(PIP) install --disable-pip-version-check --requirement requirements.txt --target "$(ARTIFACTS_DIR)"
