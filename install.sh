#!/bin/bash
# ============================================================
# Epic Kiosk - 自动驾驶领取系统（本地部署版）
# ============================================================
# GitHub: https://github.com/10000ge10000/epic-kiosk
# 公益站点: https://epic.910501.xyz/
# ============================================================
#
# 使用方式：
#   1. 克隆项目后，在项目目录执行: ./install.sh
#   2. 或一键部署: curl -fsSL https://raw.githubusercontent.com/10000ge10000/epic-kiosk/main/install.sh | bash
#
# ============================================================

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# 打印标题
print_header() {
    echo -e "${CYAN}"
    echo "============================================"
    echo "   Epic Kiosk - 自动驾驶领取系统"
    echo "   本地部署版"
    echo "============================================"
    echo -e "${NC}"
}

print_step() {
    echo -e "\n${GREEN}▶ $1${NC}\n"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查系统架构
check_arch() {
    ARCH=$(uname -m)
    case $ARCH in
        x86_64|amd64)
            print_success "系统架构: x86_64"
            ;;
        aarch64|arm64)
            print_success "系统架构: ARM64"
            ;;
        *)
            print_error "不支持的架构: $ARCH"
            exit 1
            ;;
    esac
}

# 检查 Docker 是否安装
check_docker() {
    if command -v docker &> /dev/null; then
        DOCKER_VERSION=$(docker --version 2>/dev/null || echo "未知版本")
        print_success "Docker: $DOCKER_VERSION"
        return 0
    else
        return 1
    fi
}

# 检查 Docker Compose 是否可用
check_docker_compose() {
    if docker compose version &> /dev/null; then
        COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "已安装")
        print_success "Docker Compose: $COMPOSE_VERSION"
        return 0
    else
        return 1
    fi
}

# 显示 Docker 安装命令
show_docker_install_commands() {
    echo ""
    echo -e "${YELLOW}请先安装 Docker，以下是一键安装命令：${NC}"
    echo ""
    echo -e "${CYAN}【国内服务器】${NC}"
    echo -e "  curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun"
    echo ""
    echo -e "${CYAN}【海外服务器】${NC}"
    echo -e "  curl -fsSL https://get.docker.com | bash"
    echo ""
    echo -e "${YELLOW}安装完成后，请重新运行此脚本${NC}"
    echo ""
}

# 检查是否在项目目录中
check_project_directory() {
    # 检查关键文件是否存在
    if [ -f "docker-compose.yml" ] && [ -f "Dockerfile" ] && [ -f "Dockerfile.worker" ]; then
        return 0
    else
        return 1
    fi
}

# 克隆项目（仅在非项目目录时执行）
clone_project() {
    print_step "获取项目代码"

    # 如果已经在项目目录中，跳过克隆
    if check_project_directory; then
        print_success "已在项目目录中，跳过克隆"
        PROJECT_DIR=$(pwd)
        return 0
    fi

    # 检查 git
    if ! command -v git &> /dev/null; then
        print_info "安装 git..."
        if command -v apt-get &> /dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq git
        elif command -v yum &> /dev/null; then
            sudo yum install -y -q git
        fi
    fi

    # 检测是否通过 curl | bash 运行（管道模式）
    if [ -t 0 ]; then
        # 交互模式：询问用户部署目录
        echo -e "${CYAN}请选择部署位置：${NC}"
        echo "  1) 当前目录 ($(pwd))"
        echo "  2) 自定义路径"
        echo ""
        read -p "请选择 [1/2, 默认1]: " choice < /dev/tty
        choice=${choice:-1}

        case $choice in
            1)
                PROJECT_DIR=$(pwd)
                ;;
            2)
                read -p "请输入部署路径: " custom_path < /dev/tty
                if [ -z "$custom_path" ]; then
                    print_error "路径不能为空"
                    exit 1
                fi
                PROJECT_DIR="$custom_path"
                mkdir -p "$PROJECT_DIR"
                ;;
            *)
                print_error "无效选择"
                exit 1
                ;;
        esac

        print_info "部署目录: $PROJECT_DIR"

        # 检查目录是否已有项目
        if [ -d "$PROJECT_DIR/.git" ]; then
            print_warning "目录 $PROJECT_DIR 已有 Git 项目"
            read -p "是否更新代码? [Y/n]: " update_code < /dev/tty
            update_code=${update_code:-Y}

            if [[ "$update_code" =~ ^[Yy]$ ]]; then
                cd "$PROJECT_DIR"
                git pull origin main 2>/dev/null || print_warning "更新失败，继续使用现有代码"
            fi
        else
            # 目录不为空且不是 Git 项目，创建子目录
            if [ "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
                print_info "当前目录不为空，创建 epic-kiosk 子目录..."
                PROJECT_DIR="$PROJECT_DIR/epic-kiosk"
            fi
            print_info "克隆项目到 $PROJECT_DIR ..."
            git clone -b main https://github.com/10000ge10000/epic-kiosk.git "$PROJECT_DIR"
        fi
    else
        # 管道模式：自动创建目录并克隆
        PROJECT_DIR="$(pwd)/epic-kiosk"
        print_info "管道模式：自动创建目录 $PROJECT_DIR"

        if [ -d "$PROJECT_DIR/.git" ]; then
            print_info "更新现有项目..."
            cd "$PROJECT_DIR" && git pull origin main 2>/dev/null || print_warning "更新失败，继续使用现有代码"
        else
            print_info "克隆项目..."
            git clone -b main https://github.com/10000ge10000/epic-kiosk.git "$PROJECT_DIR"
        fi
    fi

    cd "$PROJECT_DIR"
    print_success "项目准备完成"
}

# API Key 配置向导
configure_api_key() {
    print_step "配置 SiliconFlow API Key"

    echo -e "${CYAN}SiliconFlow 是什么？${NC}"
    echo "  SiliconFlow 是国内常用的 OpenAI 兼容模型 API 平台，兼容 /v1/chat/completions 接口"
    echo "  本项目默认使用 SiliconFlow 的文本模型和视觉模型处理流程判断与 hCaptcha"
    echo ""
    echo -e "${GREEN}获取 API Key 步骤：${NC}"
    echo ""
    echo -e "${CYAN}1. 访问 SiliconFlow 分享链接${NC}"
    echo -e "   ${YELLOW}https://cloud.siliconflow.cn/i/OVI2n57p${NC}"
    echo ""
    echo -e "${CYAN}2. 登录 SiliconFlow 账号${NC}"
    echo "   如果页面要求验证，请按 SiliconFlow 当前流程完成验证"
    echo ""
    echo -e "${CYAN}3. 创建 API Key${NC}"
    echo "   进入 API Key 管理页创建新密钥"
    echo "   访问 https://cloud.siliconflow.cn/i/OVI2n57p"
    echo "   复制生成的密钥（通常以 sk- 开头）；通过分享链接注册认证后，双方可获得 16 元代金券"
    echo ""

    # 检查 .env 是否已配置 API_KEY
    if [ -f ".env" ]; then
        current_key=$(grep -E "^API_KEY=" .env | head -1 | sed 's/API_KEY=//')
        if [[ "$current_key" =~ ^sk- && "$current_key" != "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" ]]; then
            print_success "已检测到 API Key: ${current_key:0:10}...${current_key: -4}"
            read -p "是否使用现有 Key? [Y/n]: " use_existing < /dev/tty
            use_existing=${use_existing:-Y}

            if [[ "$use_existing" =~ ^[Yy]$ ]]; then
                API_KEY="$current_key"
                return 0
            fi
        fi
    fi

    # 输入 API Key（从 /dev/tty 读取，支持 curl | bash 管道运行）
    while true; do
        echo ""
        read -p "请输入你的 SiliconFlow API Key (sk-xxx): " api_key < /dev/tty

        if [[ -z "$api_key" ]]; then
            print_error "API Key 不能为空"
            continue
        fi

        if [[ ! "$api_key" =~ ^sk- ]]; then
            print_warning "SiliconFlow API Key 通常以 sk- 开头，请确认"
        fi

        echo ""
        echo -e "你输入的: ${YELLOW}${api_key:0:10}...${api_key: -4}${NC}"
        read -p "确认无误? [Y/n]: " confirm_key < /dev/tty
        confirm_key=${confirm_key:-Y}

        if [[ "$confirm_key" =~ ^[Yy] ]]; then
            API_KEY="$api_key"
            break
        fi
    done

    print_success "API Key 已设置"
}

# 配置并启动服务
deploy_service() {
    print_step "部署服务"

    cd "$PROJECT_DIR"

    # 写入 .env，避免把真实 key 固化到 docker-compose.yml
    print_info "配置 API Key..."
    if [ -f ".env.example" ] && [ ! -f ".env" ]; then
        cp .env.example .env
    fi

    if [ -f ".env" ]; then
        if grep -qE "^API_KEY=" .env; then
            if [[ "$OSTYPE" == "darwin"* ]]; then
                sed -i '' "s|^API_KEY=.*|API_KEY=$API_KEY|g" .env
            else
                sed -i "s|^API_KEY=.*|API_KEY=$API_KEY|g" .env
            fi
        else
            printf "
API_KEY=%s
" "$API_KEY" >> .env
        fi

        if ! grep -qE "^API_BASE_URL=" .env; then
            printf "API_BASE_URL=https://api.siliconflow.cn/v1
" >> .env
        fi
        print_success "API Key 已写入 .env"
    else
        print_error ".env 不存在且无法创建"
        exit 1
    fi

    # 本地构建并启动（不拉取云端镜像）
    print_info "开始构建镜像（首次约需 5-10 分钟）..."
    print_info "正在构建 Web 服务..."
    docker compose build web 2>&1 | tail -5

    print_info "正在构建 Worker 服务..."
    docker compose build worker 2>&1 | tail -5

    # 启动服务
    print_info "启动服务..."
    docker compose up -d

    # 等待服务启动
    print_info "等待服务启动..."
    sleep 5

    # 检查服务状态
    if docker compose ps 2>/dev/null | grep -q "Up\|running"; then
        print_success "服务启动成功"
    else
        print_warning "服务状态异常，查看日志..."
        docker compose logs --tail 20
    fi
}

# 显示完成信息
show_complete() {
    # 只显示公网 IPv4 地址
    PUBLIC_IP=$(curl -4 -s --connect-timeout 3 ifconfig.me 2>/dev/null)

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}   部署完成！${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "${CYAN}项目目录:${NC} $PROJECT_DIR"
    echo ""
    echo -e "${CYAN}访问地址:${NC}"
    if [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "127.0.0.1" ]]; then
        echo -e "  公网: ${YELLOW}http://$PUBLIC_IP:18000${NC}"
    else
        echo -e "  公网: ${YELLOW}未检测到公网 IPv4，请检查服务器网络${NC}"
    fi
    echo ""
    echo -e "${CYAN}常用命令:${NC}"
    echo "  查看状态: cd $PROJECT_DIR && docker compose ps"
    echo "  查看日志: docker logs epic-worker -f"
    echo "  重启服务: cd $PROJECT_DIR && docker compose restart"
    echo "  更新代码: cd $PROJECT_DIR && git pull && docker compose up -d --build"
    echo ""
    echo -e "${CYAN}相关链接:${NC}"
    echo "  公益站点: https://epic.910501.xyz/"
    echo "  GitHub: https://github.com/10000ge10000/epic-kiosk"
    echo "  B 站: https://space.bilibili.com/59438380"
    echo ""
}

# 主函数
main() {
    print_header

    # 检查系统架构
    print_step "系统检查"
    check_arch

    # 检查 Docker
    print_info "检查 Docker..."
    if ! check_docker; then
        print_error "未检测到 Docker"
        show_docker_install_commands
        exit 1
    fi

    # 检查 Docker Compose
    print_info "检查 Docker Compose..."
    if ! check_docker_compose; then
        print_error "Docker Compose 不可用"
        print_info "请更新 Docker 到最新版本"
        exit 1
    fi

    # 克隆或确认项目目录
    clone_project

    # 配置 API Key
    configure_api_key

    # 部署服务
    deploy_service

    # 完成
    show_complete
}

# 运行
main "$@"
