from logging import Logger
from typing import List

from stg.stg_settings_repository import EtlSetting, StgEtlSettingsRepository
from lib import PgConnect
from lib.dict_util import json2str
from psycopg import Connection
from psycopg.rows import class_row
from pydantic import BaseModel
from datetime import datetime

class OutboxObj(BaseModel):
    id: int
    event_ts: datetime
    event_type: str
    event_value: str


class OutboxOriginRepository:
    def __init__(self, pg: PgConnect) -> None:
        self._db = pg

    def list_outbox(self, outbox_threshold: int, limit: int) -> List[OutboxObj]:
        with self._db.client().cursor(row_factory=class_row(OutboxObj)) as cur:
            cur.execute(
                """
                    SELECT id, event_ts, event_type, event_value
                    FROM outbox
                    WHERE id > %(threshold)s --Пропускаем те объекты, которые уже загрузили.
                    ORDER BY id ASC --Обязательна сортировка по id, т.к. id используем в качестве курсора.
                    LIMIT %(limit)s; --Обрабатываем только одну пачку объектов.
                """, {
                    "threshold": outbox_threshold,
                    "limit": limit
                }
            )
            objs = cur.fetchall()
        return objs


class OutboxDestRepository:

    def insert_outbox(self, conn: Connection, outbox: OutboxObj) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO stg.bonussystem_events(id, event_ts, event_type, event_value)
                    VALUES (%(id)s, %(event_ts)s, %(event_type)s, %(event_value)s)
                    ON CONFLICT (id) DO NOTHING;
                """,
                {
                    "id": outbox.id,
                    "event_ts": outbox.event_ts,
                    "event_type": outbox.event_type,
                    "event_value": outbox.event_value
                },
            )


class OutboxLoader:
    WF_KEY = "bonus_system_outbox_origin_to_stg_workflow"
    LAST_LOADED_ID_KEY = "last_loaded_id"
    OUTBOX_LIMIT = 1000  # инкрементальную загрузка порциями не более USER_LIMIT строк за раз

    def __init__(self, pg_origin: PgConnect, pg_dest: PgConnect, log: Logger) -> None:
        self.pg_dest = pg_dest
        self.origin = OutboxOriginRepository(pg_origin)
        self.stg = OutboxDestRepository()
        self.settings_repository = StgEtlSettingsRepository()
        self.log = log

    def load_outbox(self):
        # открываем транзакцию.
        # Транзакция будет закоммичена, если код в блоке with пройдет успешно (т.е. без ошибок).
        # Если возникнет ошибка, произойдет откат изменений (rollback транзакции).
        with self.pg_dest.connection() as conn:

            # Прочитываем состояние загрузки
            # Если настройки еще нет, заводим ее.
            wf_setting = self.settings_repository.get_setting(conn, self.WF_KEY)
            if not wf_setting:
                wf_setting = EtlSetting(id=0, workflow_key=self.WF_KEY, workflow_settings={self.LAST_LOADED_ID_KEY: -1})

            # Вычитываем очередную пачку объектов.
            last_loaded = wf_setting.workflow_settings[self.LAST_LOADED_ID_KEY]
            load_queue = self.origin.list_outbox(last_loaded, self.OUTBOX_LIMIT)
            self.log.info(f"Found {len(load_queue)} outbox to load.")
            if not load_queue:
                self.log.info("Quitting.")
                return

            # Сохраняем объекты в базу dwh.
            for outbox in load_queue:
                self.stg.insert_outbox(conn, outbox)

            # Сохраняем прогресс.
            # Мы пользуемся тем же connection, поэтому настройка сохранится вместе с объектами,
            # либо откатятся все изменения целиком.
            wf_setting.workflow_settings[self.LAST_LOADED_ID_KEY] = max([t.id for t in load_queue])
            wf_setting_json = json2str(wf_setting.workflow_settings)  # Преобразуем к строке, чтобы положить в БД.
            self.settings_repository.save_setting(conn, wf_setting.workflow_key, wf_setting_json)

            self.log.info(f"Load finished on {wf_setting.workflow_settings[self.LAST_LOADED_ID_KEY]}")
