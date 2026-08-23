import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from strategy_lab.execution import process_runtime
from strategy_lab.models import StrategyRuntime
from strategy_lab.training import claim_next_training, process_training_run

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run queued backtests and active strategy executions."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=5.0)

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Strategy worker started"))
        while True:
            close_old_connections()
            run = claim_next_training()
            if run:
                logger.info("Processing strategy training %s", run.id)
                process_training_run(run)
            for runtime in StrategyRuntime.objects.filter(
                status=StrategyRuntime.Status.ACTIVE
            ).select_related("strategy", "user"):
                process_runtime(runtime)
            if options["once"]:
                return
            time.sleep(max(1, options["interval"]))
