DB_CONTAINER=filoutil-db
DB_USER=postgres
DB_NAME=filoutil
BACKUP_FILE=backup.sql

.PHONY: db-shell db-backup db-restore

# Access the database shell
db-shell:
	docker exec -it $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)

# Backup the database
db-backup:
	docker exec $(DB_CONTAINER) pg_dump -U $(DB_USER) $(DB_NAME) > $(BACKUP_FILE)
	@echo "Backup saved to $(BACKUP_FILE)"

# Restore the database from backup
db-restore:
	@if [ ! -f $(BACKUP_FILE) ]; then echo "Error: $(BACKUP_FILE) not found"; exit 1; fi
	cat $(BACKUP_FILE) | docker exec -i $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)
	@echo "Database restored from $(BACKUP_FILE)"
