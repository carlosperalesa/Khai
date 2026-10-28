# Controlador Dolphin para Khai (por defecto: Google Cloud Spot en ./gcloud; legado Azure en ./azure).

SHELL := powershell.exe
.SHELLFLAGS := -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command

GCLOUD_SCRIPT := ./gcloud/gcloud-ollama.ps1
AZURE_SCRIPT  := ./azure/azure-ollama.ps1

.PHONY: help on on-low on-high off status azure-help azure-on azure-off azure-status

help:
	@& '$(GCLOUD_SCRIPT)' help

on:
	@& '$(GCLOUD_SCRIPT)' on-low

on-low:
	@& '$(GCLOUD_SCRIPT)' on-low

on-high:
	@& '$(GCLOUD_SCRIPT)' on-high

off:
	@& '$(GCLOUD_SCRIPT)' off

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
