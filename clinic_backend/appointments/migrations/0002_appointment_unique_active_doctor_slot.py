# Generated manually for appointment slot uniqueness constraint

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("PENDING", "APPROVED"))),
                fields=("doctor", "appointment_date", "appointment_time"),
                name="unique_active_doctor_slot",
            ),
        ),
    ]
