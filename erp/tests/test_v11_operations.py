import sqlite3
from contextlib import closing
import tempfile
from pathlib import Path
from django.core.management import call_command
from django.test.utils import CaptureQueriesContext
from django.db import connection
from erp import models as m
from .base import ERPTestCase


class OperationsTests(ERPTestCase):
    def test_health_checks_database_without_exposing_configuration(self):
        response=self.client.get('/healthz/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'status':'ok','database':'ok'})

    def test_dashboard_counts_all_tasks_beyond_display_limit(self):
        for i in range(25):self.make(m.Measurement,customer=self.customer,due_date='2026-10-01')
        self.client.force_login(self.sales)
        response=self.client.get('/')
        self.assertEqual(response.context['task_count'],25)
        self.assertEqual(response.context['risk_count'],25)
        self.assertEqual(len(response.context['tasks']),7)

    def test_finance_aggregates_whole_scope_with_bounded_detail(self):
        for i in range(55):self.order(status='confirmed')
        self.client.force_login(self.finance)
        response=self.client.get('/modules/finance/')
        self.assertEqual(response.context['receivable_total'],55000)
        self.assertEqual(len(response.context['receivables']),50)

    def test_permissions_reuse_memberships_within_one_http_request(self):
        self.client.force_login(self.sales)
        with CaptureQueriesContext(connection) as queries:self.client.get('/modules/customers/')
        count=sum('FROM "erp_membership"' in x['sql'] for x in queries)
        self.assertLessEqual(count,2)

    def test_sqlite_online_backup_preserves_source_and_refuses_overwrite(self):
        # A file-backed independent source avoids backing up this TestCase's uncommitted transaction.
        if connection.vendor!='sqlite':self.skipTest('SQLite backup is intentionally backend-specific')
        from erp.management.commands.backup_db import sqlite_backup
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.sqlite3'; target=Path(folder)/'copy.sqlite3'
            with closing(sqlite3.connect(source)) as db:db.execute('create table proof (value text)');db.execute("insert into proof values ('完整备份')");db.commit()
            sqlite_backup(source,target)
            with closing(sqlite3.connect(target)) as db:self.assertEqual(db.execute('select value from proof').fetchone()[0],'完整备份')
            with self.assertRaises(FileExistsError):sqlite_backup(source,target)
