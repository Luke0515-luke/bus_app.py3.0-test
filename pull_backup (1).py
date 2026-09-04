import subprocess
import os

def pull_backup():
    # 配置仓库信息
    github_token = os.environ.get("GITHUB_TOKEN")
    branch_name = 'master'  # 替换为你要拉取的分支
    local_dir = '/opt/render/project/data/'  # 目标目录（数据存储目录）

    # 设置认证信息（GitHub Personal Access Token）
    #username = 'xxxxxxxx'  # 你的 GitHub 用户名
    #token = 'your_personal_access_token'  # 你的 GitHub Personal Access Token (PAT)

    # 拼接带认证信息的仓库 URL
    remote_url = f'https://{github_token}@github.com/Luke0515-luke/bus_app.py3.0backup.git'  # 去掉 https:// 前缀

    # 检查本地目录是否存在
    if not os.path.exists(local_dir):
        print(f"目录 {local_dir} 不存在，正在克隆仓库到该目录...")
        # 克隆仓库
        command = ['git', 'clone', '--branch', branch_name, remote_url, local_dir]
        try:
            subprocess.run(["git", "init"], check=True)
            subprocess.run(["git", "config", "user.name", "luke"])
            subprocess.run(["git", "config", "user.email", "0515luke@gmail.com"])
            subprocess.run(command, check=True)
            print(f"成功克隆仓库到 {local_dir}")
        except subprocess.CalledProcessError as e:
            print(f"克隆仓库时出错: {e}")
    else:
        print(f"目录 {local_dir} 已存在，正在拉取远程更改...")
        # 拉取远程更新
        command = ['git', '-C', local_dir, 'pull', 'origin', branch_name]
        try:
            subprocess.run(["git", "init"], check=True)
            subprocess.run(["git", "config", "user.name", "luke"])
            subprocess.run(["git", "config", "user.email", "0515luke@gmail.com"])
            subprocess.run(command, check=True)
            print(f"成功拉取远程仓库的更新到 {local_dir}")
        except subprocess.CalledProcessError as e:
            print(f"拉取更新时出错: {e}")

    print("操作完成！")
