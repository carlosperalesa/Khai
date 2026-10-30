# Controlador Dolphin para Khai (por defecto: Google Cloud Spot en ./gcloud; legado Azure en ./azure).

SHELL := powershell.exe
.SHELLFLAGS := -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command

GCLOUD_SCRIPT := ./gcloud/gcloud-ollama.ps1
AZURE_SCRIPT  := ./azure/azure-ollama.ps1

.PHONY: help nano low mid high on on-low on-mid on-high off status azure-help azure-on azure-off azure-status

help:
	@& '$(GCLOUD_SCRIPT)' help

nano:
	@& '$(GCLOUD_SCRIPT)' nano

low:
	@& '$(GCLOUD_SCRIPT)' low

mid:
	@& '$(GCLOUD_SCRIPT)' mid

high:
	@& '$(GCLOUD_SCRIPT)' high

on:
	@& '$(GCLOUD_SCRIPT)' mid

on-low:
	@& '$(GCLOUD_SCRIPT)' low

on-mid:
	@& '$(GCLOUD_SCRIPT)' mid

on-high:
	@& '$(GCLOUD_SCRIPT)' high

off:
	@& '$(GCLOUD_SCRIPT)' nano

status:
	@& '$(GCLOUD_SCRIPT)' status

azure-help:
	@& '$(AZURE_SCRIPT)' help

azure-on:
	@& '$(AZURE_SCRIPT)' on

azure-off:
	@& '$(AZURE_SCRIPT)' off

azure-status:
	@& '$(AZURE_SCRIPT)' status
