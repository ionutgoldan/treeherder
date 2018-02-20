# -*- coding: utf-8 -*-
# Generated by Django 1.11.10 on 2018-02-20 08:41
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('perf', '0004_add_per_push_performance_datum_index'),
    ]

    operations = [
        migrations.RunSQL('''
        UPDATE performance_signature
        SET alert_threshold = 8
        WHERE
            suite LIKE "String" AND
            test LIKE "PerfStrip%" AND
            framework_id = (
                SELECT id
                FROM performance_framework
                WHERE name LIKE "platform_microbench"
                LIMIT 1
            )''', reverse_sql='''
        UPDATE performance_signature
        SET alert_threshold = NULL
        WHERE
            suite LIKE "String" AND
            test LIKE "PerfStrip%" AND
            framework_id = (
                SELECT id
                FROM performance_framework
                WHERE name LIKE "platform_microbench"
                LIMIT 1
            )''')
    ]
