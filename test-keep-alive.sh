#!/bin/bash

# Test script to manually check the keep-alive functionality
# This mimics what the GitHub Actions workflow does

echo "🔍 Testing Keep-Alive Functionality"
echo "=================================="
echo ""

URL="https://telegram-y5ic.onrender.com/health"
echo "📡 Pinging: $URL"
echo "⏰ Time: $(date)"
echo ""

# Test the health endpoint
echo "Testing /health endpoint..."
response=$(curl -s -w "%{http_code}" -o /tmp/health_response.txt "$URL")
body=$(cat /tmp/health_response.txt)

echo "HTTP Status Code: $response"
echo "Response Body: $body"

if [ "$response" = "200" ]; then
    echo "✅ Health endpoint is working correctly"
else
    echo "❌ Health endpoint failed with status: $response"
    
    # Try main endpoint as fallback
    echo ""
    echo "Trying main endpoint as fallback..."
    main_response=$(curl -s -w "%{http_code}" -o /tmp/main_response.txt "https://telegram-y5ic.onrender.com/")
    main_body=$(cat /tmp/main_response.txt | head -c 200)
    
    echo "Main endpoint status: $main_response"
    echo "Main endpoint response (first 200 chars): $main_body"
    
    if [ "$main_response" = "200" ]; then
        echo "✅ Main endpoint is working"
    else
        echo "❌ Both endpoints failed"
    fi
fi

echo ""
echo "🔍 To check GitHub Actions workflow runs:"
echo "   Visit: https://github.com/facad4/telegram/actions"
echo ""
echo "💡 The workflow should run every 10 minutes automatically."
echo "   You can also trigger it manually from the Actions tab."

# Clean up temp files
rm -f /tmp/health_response.txt /tmp/main_response.txt