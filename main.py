"""Firebase Cloud Functions 진입점.

기존 GitHub Actions poll.yml(python -m insider_signal.cli run --once)을 대체하는
스케줄 함수. 파이프라인 조립 로직은 cli.py와 동일하게 insider_signal 패키지를
그대로 재사용하고, Cloud Functions의 상태 없는(stateless) 환경에 맞춰 이력 DB
(SQLite)만 Cloud Storage에서 내려받고/올려보내는 얇은 wrapper 역할만 한다.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from firebase_functions import options, params, scheduler_fn
from google.cloud import storage

from insider_signal.config import load_settings
from insider_signal.edgar_client import SECEdgarClient
from insider_signal.history import HistoryStore
from insider_signal.notifier import build_notifier
from insider_signal.poller import run_once

# GitHub Actions secrets(SEC_EDGAR_CONTACT, SLACK_WEBHOOK_URL)를 대체. 배포 후
# `firebase functions:secrets:set <NAME>`으로 값을 등록하면 런타임에 같은 이름의
# 환경변수로 주입되어 config.py의 os.getenv(...) 호출부는 그대로 동작한다.
SEC_EDGAR_CONTACT = params.SecretParam("SEC_EDGAR_CONTACT")
SLACK_WEBHOOK_URL = params.SecretParam("SLACK_WEBHOOK_URL")

# .firebaserc의 프로젝트 ID와 짝을 맞춰야 함 (프로젝트 ID를 바꾸면 여기도 같이 바꿀 것).
HISTORY_BUCKET_NAME = "insider-signal-me01-history"
HISTORY_BLOB_NAME = "insider_signal.db"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@scheduler_fn.on_schedule(
    schedule="7 * * * *",
    timezone="Etc/UTC",
    secrets=[SEC_EDGAR_CONTACT, SLACK_WEBHOOK_URL],
    timeout_sec=540,
    memory=options.MemoryOption.MB_256,
    region="us-central1",
)
def poll(event: scheduler_fn.ScheduledEvent) -> None:
    db_path = Path(tempfile.gettempdir()) / HISTORY_BLOB_NAME
    bucket = storage.Client().bucket(HISTORY_BUCKET_NAME)
    blob = bucket.blob(HISTORY_BLOB_NAME)
    if blob.exists():
        blob.download_to_filename(str(db_path))
    else:
        logger.info("이력 DB가 아직 없음 (최초 실행): %s", db_path)

    settings = load_settings()
    settings.history_db_path = db_path

    client = SECEdgarClient(contact=settings.sec_edgar_contact)
    notifier = build_notifier(settings)

    with HistoryStore(settings.history_db_path) as history:
        total = run_once(client=client, history=history, notifier=notifier, settings=settings)

    blob.upload_from_filename(str(db_path))
    logger.info("완료: 신호 %d건 발생", total)
