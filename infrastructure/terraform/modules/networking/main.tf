data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── VPC ──────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = merge(local.tags, { Name = "${var.project}-${var.environment}-vpc" })
}

# ── Subnets ───────────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  # Instances in these subnets do NOT get a public IP automatically.
  # Only the ALB lives here; ECS tasks are in private subnets.
  map_public_ip_on_launch = false

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-public-${count.index + 1}" })
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-private-${count.index + 1}" })
}

# ── Internet Gateway + Public Route Table ────────────────────────────

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.project}-${var.environment}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-public-rt" })
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ── NAT Gateway + Private Route Table ───────────────────────────────
# Single NAT Gateway for cost efficiency in dev. For production, one per AZ.

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = merge(local.tags, { Name = "${var.project}-${var.environment}-nat-eip" })
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = merge(local.tags, { Name = "${var.project}-${var.environment}-nat" })
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-private-rt" })
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ── Security Groups ───────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "${var.project}-${var.environment}-alb-sg"
  description = "Allow inbound HTTP from internet to ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-alb-sg" })
}

resource "aws_security_group" "backend" {
  name        = "${var.project}-${var.environment}-backend-sg"
  description = "Allow inbound traffic from ALB to backend ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "FastAPI from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Allow all outbound (NAT → internet for Claude API / ECR pulls)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-backend-sg" })
}

resource "aws_security_group" "frontend" {
  name        = "${var.project}-${var.environment}-frontend-sg"
  description = "Allow inbound traffic from ALB to frontend ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Next.js from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-frontend-sg" })
}

resource "aws_security_group" "rds" {
  name        = "${var.project}-${var.environment}-rds-sg"
  description = "Allow inbound PostgreSQL from backend ECS tasks only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from backend tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.backend.id]
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-rds-sg" })
}
