"""
保護者ページとアイコンのホームを、事業所ごとにオン／オフする（配備の作業から。画面では 施設設定 → 使う機能）。
  python manage.py family_page --facility 1          # 両方オン
  python manage.py family_page --facility 1 --off    # 両方オフ（保護者のリンク・記録は消さない）
"""
from django.core.management.base import BaseCommand, CommandError

from facilities.models import Facility


class Command(BaseCommand):
    help = '事業所の「ホームをアイコンの並びにする」「保護者ページを使う」をオン／オフする'

    def add_arguments(self, parser):
        parser.add_argument('--facility', type=int, required=True, help='施設 ID')
        parser.add_argument('--off', action='store_true', help='オフにする')

    def handle(self, *args, **o):
        facility = Facility.objects.filter(pk=o['facility']).first()
        if facility is None:
            raise CommandError(f'施設 ID {o["facility"]} はありません')
        on = not o['off']
        facility.home_icons = on
        facility.use_family_page = on
        facility.save(update_fields=['home_icons', 'use_family_page', 'updated_at'])
        note = '' if (facility.use_line or not on) else '（LINE連携はオフです。職員の画面と保護者アプリのアカウントは使えます。公式 LINE から開く保護者ページは、LINE連携をオンにするまで開けません）'
        self.stdout.write(self.style.SUCCESS(
            f'施設 ID {facility.pk}：ホームのアイコン・保護者ページを{"オン" if on else "オフ"}にしました{note}'))
