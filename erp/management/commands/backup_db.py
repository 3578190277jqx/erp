"""Consistent SQLite online backup; PostgreSQL deployments use pg_dump."""
import sqlite3
from contextlib import closing
from pathlib import Path
from django.core.management.base import BaseCommand,CommandError
from django.db import connection


def sqlite_backup(source,target):
    target=Path(target).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents accidental replacement of any existing file.
    with target.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(str(source))) as origin,closing(sqlite3.connect(str(target))) as destination:
            origin.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise CommandError('备份完整性检查失败。')
    except Exception:
        target.unlink(missing_ok=True)
        raise


class Command(BaseCommand):
    help='在线备份 SQLite 到新文件；不会覆盖已有备份。PostgreSQL 请使用 pg_dump。'
    def add_arguments(self,parser):
        parser.add_argument('destination')
    def handle(self,*args,**options):
        if connection.vendor!='sqlite':raise CommandError('当前为 PostgreSQL，请使用 pg_dump 备份并验证恢复。')
        try:sqlite_backup(connection.settings_dict['NAME'],options['destination'])
        except FileExistsError:raise CommandError('目标文件已存在，请使用新的备份文件名。')
        self.stdout.write(self.style.SUCCESS('备份完成，SQLite integrity_check=ok。'))
