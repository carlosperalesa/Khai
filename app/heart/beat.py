import json
import time
from app.common.database import connection, migrate
from app.heart.vital_signs import VitalSigns
from app.heart.diagnosis import Diagnosis


class Beat:
    def __init__(self, root, events):
        self.path = root/'heart'/'heart.sqlite3'
        self.vitals, self.diagnosis, self.events = VitalSigns(root),Diagnosis(root),events
        migrate(self.path, [['CREATE TABLE IF NOT EXISTS beats(created_at REAL,report TEXT)']])

    def check_integrity(self):
        return self.diagnosis.inspect()

    def run_diagnostic_fast(self):
        return {'vitals':self.vitals.sample(),'integrity':self.check_integrity()}

    async def _fast_beat_basal(self):
        report = self.run_diagnostic_fast()
        with connection(self.path) as conn:
            conn.execute('INSERT INTO beats VALUES (?,?)',(time.time(),json.dumps(report)))
        self.events.emit('heart','beat',metadata={'status':report['vitals']['status']})
        return report
