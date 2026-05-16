"""Helper pra criar o primeiro admin. Roda 1x e deleta."""
from auth import create_first_admin
"""Helper pra criar o primeiro admin. Roda 1x e deleta."""
import os
from dotenv import load_dotenv

# Carrega .env ANTES de importar auth
load_dotenv()
create_first_admin(
    email="caiovicenterj@gmail.com",
    password="33383609",
    name="Caio Vicente",
)