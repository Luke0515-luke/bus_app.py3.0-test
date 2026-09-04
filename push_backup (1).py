import subprocess
import os

# 設定單次推送的總大小上限 (建議設為 15MB，以確保在 Render/Cloudflare 環境下絕對安全)
BATCH_SIZE_LIMIT = 15 * 1024 * 1024
github_token = os.environ.get("GITHUB_TOKEN")

def run_cmd(args, check=True):
    """執行指令的輔助函式，方便除錯"""
    try:
        subprocess.run(args, check=check)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 指令失敗: {' '.join(args)}\n原因: {e}", flush=True)
        return False

def git_push_backup(backup_path):
    print(f"準備開始推送")
    try:
        os.chdir(backup_path)
        repo_url =f'https://{github_token}@github.com/Luke0515-luke/bus_app.py3.0backup.git'
        
        subprocess.run(["git", "config", "user.name", "luke"], check=True)
        subprocess.run(["git", "config", "user.email", "0515luke@gmail.com"], check=True)

        # 1. 初始化檢查 (只在第一次執行 init，保留 .git 資料夾以達成增量備份)
        if not os.path.exists(os.path.join(backup_path, ".git")):
            subprocess.run(["git", "init"], check=True)
            subprocess.run(["git", "config", "user.name", "luke"], check=True)
            subprocess.run(["git", "config", "user.email", "0515luke@gmail.com"], check=True)
            subprocess.run(["git", "branch", "-M", "master"], check=True)
            subprocess.run(["git", "remote", "add", "origin", repo_url], check=True)
            # 嘗試拉取遠端以避免衝突 (若遠端是空的會失敗，所以 check=False)
            subprocess.run(["git", "pull", "origin", "master"], stderr=subprocess.DEVNULL, check=False)

        # 2. 確保遠端 URL 是最新的 (防止 Token 變更後失效)
        subprocess.run(["git", "remote", "set-url", "origin", repo_url], check=True)

        # --- [核心修改] 分批推送邏輯 ---
        
        # 取得所有變更檔案的列表 (包含新增、修改、刪除)
        # git status --porcelain 輸出格式範例: "M  file.txt" 或 "?? new.py"
        print("🔍 掃描檔案變更中...", flush=True)
        status_output = subprocess.check_output(["git", "status", "--porcelain"], text=True)
        
        # 解析變更列表
        files_to_process = []
        for line in status_output.splitlines():
            if len(line) > 3:
                # 去除前3個字元的狀態碼，並去除頭尾空白或引號
                filename = line[3:].strip().strip('"')
                files_to_process.append(filename)

        if not files_to_process:
            print("✅ 檔案無變動，無需推送。", flush=True)
            return

        print(f"📦 共有 {len(files_to_process)} 個檔案需要同步", flush=True)

        current_batch_files = []
        current_batch_size = 0
        batch_count = 1

        for filename in files_to_process:
            # 計算檔案大小
            file_size = 0
            if os.path.exists(filename):
                file_size = os.path.getsize(filename)
            # 注意：如果是被刪除的檔案，exists 會是 False，大小算 0，這沒問題

            # 檢查加入這個檔案後，是否會超過限制
            # 如果目前批次有東西，且加入新檔案會爆量 -> 先推目前的
            if current_batch_files and (current_batch_size + file_size > BATCH_SIZE_LIMIT):
                print(f"🚀 執行第 {batch_count} 批次推送 (大小: {current_batch_size/1024/1024:.2f} MB)...", flush=True)
                
                # Git Add & Commit & Push
                run_cmd(["git", "add"] + current_batch_files)
                run_cmd(["git", "commit", "-m", f"Auto Sync: Batch {batch_count}"])
                run_cmd(["git", "push", "-f", "origin", "master"])
                
                # 重置批次
                current_batch_files = []
                current_batch_size = 0
                batch_count += 1

            # 將檔案加入目前批次
            current_batch_files.append(filename)
            current_batch_size += file_size

        # --- 處理剩下的最後一批檔案 ---
        if current_batch_files:
            print(f"🚀 執行最後一批次推送 (大小: {current_batch_size/1024/1024:.2f} MB)...", flush=True)
            run_cmd(["git", "add"] + current_batch_files)
            run_cmd(["git", "commit", "-m", "Auto Sync: Final Batch"])
            run_cmd(["git", "push", "-f", "origin", "master"])

        # 為了保險起見，最後再執行一次 add --all 確保刪除操作也被同步
        # 因為上面的迴圈主要處理檔案路徑，有時對純刪除的處理可能不夠全面
        remaining_status = subprocess.check_output(["git", "status", "--porcelain"], text=True)
        if remaining_status.strip():
            print("🧹 清理剩餘的刪除操作...", flush=True)
            run_cmd(["git", "add", "--all"])
            run_cmd(["git", "commit", "-m", "Auto Sync: Cleanup"])
            run_cmd(["git", "push", "-f", "origin", "master"])

        print("✅ 所有備份已成功分批同步到 GitHub！", flush=True)
            
    except subprocess.CalledProcessError as e:
        print(f"❌ Git 推送失敗：{e}", flush=True)
    except Exception as e:
        print(f"❌ 發生錯誤：{e}", flush=True)
