DB_CONTAINER=filoutil-db
DB_USER=postgres
DB_NAME=filoutil
BACKUP_FILE=backup.sql

.PHONY: db backup restore ensure-db

# Ensure the database container is running
ensure-db:
	@if [ $$(docker ps -q -f name=$(DB_CONTAINER) | wc -l) -eq 0 ]; then \
		echo "Database container is not running. Starting..."; \
		docker compose up -d db; \
		echo "Waiting for database to be ready..."; \
		sleep 3; \
	fi

# Access the database shell
db: ensure-db
	docker exec -it $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)

# Backup the database
backup: ensure-db
	docker exec $(DB_CONTAINER) pg_dump -U $(DB_USER) $(DB_NAME) > $(BACKUP_FILE)
	@echo "Backup saved to $(BACKUP_FILE)"

# Restore the database from backup
restore: ensure-db
	@if [ ! -f $(BACKUP_FILE) ]; then echo "Error: $(BACKUP_FILE) not found"; exit 1; fi
	$(eval TIMESTAMP := $(shell date +%Y%m%d_%H%M%S))
	$(eval SAFETY_BACKUP := backup_safety_$(TIMESTAMP).sql)
	@echo "Creating safety backup to $(SAFETY_BACKUP)..."
	docker exec $(DB_CONTAINER) pg_dump -U $(DB_USER) $(DB_NAME) > $(SAFETY_BACKUP)
	@echo "Erasing existing database content..."
	docker exec $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME) -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	@echo "Restoring from $(BACKUP_FILE)..."
	cat $(BACKUP_FILE) | docker exec -i $(DB_CONTAINER) psql -U $(DB_USER) -d $(DB_NAME)
	@echo "Database restored from $(BACKUP_FILE)"
