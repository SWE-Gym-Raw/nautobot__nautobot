# Generated by Django 3.2.18 on 2023-02-23 21:44

from django.db import migrations
import django.db.models.deletion
import nautobot.extras.models.statuses


class Migration(migrations.Migration):

    dependencies = [
        ("extras", "0064_created_datetime"),
        ("circuits", "0013_alter_circuittermination__path"),
    ]

    operations = [
        migrations.AlterField(
            model_name="circuit",
            name="status",
            field=nautobot.extras.models.statuses.StatusField(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="circuits", to="extras.status"
            ),
        ),
    ]
