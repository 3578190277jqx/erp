import io
from django.core.management import call_command, get_commands
from django.test import override_settings
from django.contrib.auth.models import User
from erp import models as m
from erp.registry import MODULES
from .base import ERPTestCase


@override_settings(DEBUG=True)
class SeedTests(ERPTestCase):
    def test_seed_is_idempotent_and_does_not_overwrite_passwords(self):
        self.assertIn('seed_demo', get_commands(), 'Demo seed command must be implemented')
        output = io.StringIO()
        call_command('seed_demo', stdout=output)
        counts = {key: mod.model.objects.count() for key, mod in MODULES.items()}
        user = User.objects.get(username='demo_admin')
        original = user.password
        call_command('seed_demo', stdout=output)
        self.assertEqual(original, User.objects.get(username='demo_admin').password)
        self.assertEqual(counts, {key: mod.model.objects.count() for key, mod in MODULES.items()})
        self.assertGreater(m.Order.objects.count(), 5)

    def test_seeded_record_details_and_forms_render(self):
        self.assertIn('seed_demo', get_commands(), 'Demo seed command must be implemented')
        call_command('seed_demo', stdout=io.StringIO())
        self.client.force_login(User.objects.get(username='demo_admin'))
        for key, mod in MODULES.items():
            obj = mod.model.objects.first()
            if obj:
                with self.subTest(key=key):
                    self.assertEqual(self.client.get(f'/modules/{key}/{obj.pk}/').status_code, 200)
            if mod.creatable:
                with self.subTest(form=key):
                    self.assertEqual(self.client.get(f'/modules/{key}/new/').status_code, 200)
