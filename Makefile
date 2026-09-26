.PHONY: up down logs shell db check destroy

up:      ; docker compose up -d --build
down:    ; docker compose down
logs:    ; docker compose logs -f bot
shell:   ; docker compose exec bot sh
db:      ; docker compose exec bot python -c "import sqlite3,os,sys;[print(r) for r in sqlite3.connect(os.environ['JOBTRACK_DB']).execute(sys.argv[1])]" "$(Q)"
check:   ; docker compose exec bot jobtrack check

## removes containers, image, network and the database. nothing left behind.
destroy:
	docker compose down --rmi local --volumes --remove-orphans
	@echo "container, image and network gone."
	@echo "data still at ./data -- remove it, or destroy the dataset:"
	@echo "  rm -rf ./data      # and the dataset or volume it lives on"
