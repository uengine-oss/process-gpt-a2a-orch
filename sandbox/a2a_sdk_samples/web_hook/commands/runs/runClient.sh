# A2A 클라이언트 실행
# cd sandbox/a2a_sdk_samples/web_hook; bash .\commands\runs\runClient.sh

uv run python -m client.client --agent-url http://localhost:8000 --message "예산 승인이 필요합니다"