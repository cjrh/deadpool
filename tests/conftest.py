import logging

FORMAT = (
    "%(levelname)10s %(relativeCreated)10d %(threadName)s "
    "%(message)s [%(filename)s:%(lineno)d]"
)
logging.basicConfig(level="DEBUG", format=FORMAT)
