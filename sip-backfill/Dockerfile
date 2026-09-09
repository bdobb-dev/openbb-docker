# sip-backfill: walks the S&P 500's consolidated tick history (EODHD Tick Data
# API) into the Delta tick vault, one week at a time. See scripts/sip_backfill.py.
#
#   docker buildx build --platform linux/amd64 -f sip-backfill/Dockerfile -t openbb-sip-backfill:11.4.6 .
#
# Local-only image like openbb-eod-dump: shipped to the NAS by
#   docker save openbb-sip-backfill:11.4.6 | ssh nas '<docker> load'
FROM python:3.12-slim
# pandas<3 is tick-lab's pin (tz_localize semantics changed in 3).
RUN pip install --no-cache-dir "pandas>=2,<3" "pyarrow>=16" "deltalake>=1.0" tzdata
COPY tick-lab/tick_lab /opt/tick-lab/tick_lab
COPY scripts/sip_backfill.py /opt/scripts/sip_backfill.py
ENV SIP_WORK=/tmp/sip
ENTRYPOINT ["python", "/opt/scripts/sip_backfill.py"]
CMD ["--loop"]
