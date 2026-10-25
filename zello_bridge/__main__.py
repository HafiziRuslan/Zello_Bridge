import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os

from .stream import AsyncByteStream
from .usrp import USRPController
from .zello import ZelloController

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
log_format = os.environ.get('LOG_FORMAT', '%(asctime)s | %(levelname)s | %(name)s | %(message)s')
log_dir = os.environ.get('LOG_DIR')
log_max_bytes = int(os.environ.get('LOG_MAX_BYTES', 2 * 1024 * 1024))
log_backup_count = int(os.environ.get('LOG_BACKUP_COUNT', 5))

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)  # Root captures everything; handlers do the filtering
formatter = logging.Formatter(log_format)

# Console Handler
sh = logging.StreamHandler()
sh.setLevel(log_level)
sh.setFormatter(formatter)
root_logger.addHandler(sh)

if log_dir:
	os.makedirs(log_dir, exist_ok=True)
	# Create a separate log file for each standard level
	for level_name in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
		lvl = getattr(logging, level_name)
		h = RotatingFileHandler(os.path.join(log_dir, f'{level_name.lower()}.log'), maxBytes=log_max_bytes, backupCount=log_backup_count)
		h.setLevel(lvl)
		h.setFormatter(formatter)
		# This filter ensures the file ONLY contains records for this specific level
		h.addFilter(lambda record, target=lvl: record.levelno == target)
		root_logger.addHandler(h)

logger = logging.getLogger('__main__')


async def _main():
	loop = asyncio.get_running_loop()
	# Stream from Zello -> USRP
	zousrp = AsyncByteStream()
	# Stream from USRP -> Zello
	usrpzo = AsyncByteStream()
	usrp_ptt = asyncio.Event()
	zello_ptt = asyncio.Event()
	logger.info('Initialising Zello')
	zello = ZelloController(zousrp, usrpzo, usrp_ptt, zello_ptt)
	logger.info('Initialising USRP')
	usrp = USRPController(usrpzo, zousrp, usrp_ptt, zello_ptt)
	await asyncio.gather(
		*[
			zello.run(),
			# USRP tx
			usrp.run(),
			# Set up USRP rx
			loop.create_datagram_endpoint(lambda: usrp, local_addr=(os.environ.get('USRP_BIND'), int(os.environ.get('USRP_RXPORT', 0)))),
		]
	)
	loop.run_forever()


def main():
	asyncio.run(_main())


if __name__ == '__main__':
	main()
