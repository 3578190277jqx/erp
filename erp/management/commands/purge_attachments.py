from django.core.management.base import BaseCommand
from erp.models import Attachment
from erp.services.evidence import purge_attachment_file


class Command(BaseCommand):
    help='重试清理已授权删除、尚未完成物理文件清理的附件；保留元数据和审计。'
    def handle(self,*args,**options):
        ids=Attachment.objects.filter(deleted_at__isnull=False,purged_at__isnull=True).values_list('pk',flat=True)
        count=sum(purge_attachment_file(pk) for pk in ids.iterator())
        self.stdout.write(f'已完成{count}个待清理附件。')
