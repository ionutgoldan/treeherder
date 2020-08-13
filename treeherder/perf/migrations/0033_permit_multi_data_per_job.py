# Generated by Django 3.0.8 on 2020-08-14 12:09

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('model', '0020_textlogerror_job'),
        ('perf', '0032_add_performance_tag'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='performancedatum',
            unique_together={('repository', 'job', 'push', 'push_timestamp', 'signature')},
        ),
    ]
